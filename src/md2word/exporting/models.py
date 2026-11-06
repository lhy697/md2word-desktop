from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import threading
import time
import uuid
from collections import deque

from ..domain.models import InputSnapshot, ReaderProfile, DocumentSession, Issue, failure
from ..domain.style_models import ResolvedStyle


@dataclass(frozen=True)
class ExportSnapshot:
    input: InputSnapshot
    style: ResolvedStyle
    reader: ReaderProfile
    style_revision: int
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    local_formats: tuple = ()

    @classmethod
    def capture(cls, session: DocumentSession, style: ResolvedStyle, reader=None):
        from ..parsing import load_reader_profile
        return cls(session.snapshot(), style, reader or load_reader_profile(), session.style_revision, local_formats=session.local_formats)

    def __post_init__(self):
        if not isinstance(self.input, InputSnapshot) or not isinstance(self.style, ResolvedStyle):
            raise ValueError("Invalid export snapshot")
        if not isinstance(self.reader, ReaderProfile) or self.style_revision < 0:
            raise ValueError("Invalid export profile")
        from ..domain.local_formatting import validate_formats
        object.__setattr__(self,"local_formats",validate_formats(self.local_formats))


class TaskState(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    CHECKING = "checking"
    WRITING = "writing"
    POSTPROCESSING = "postprocessing"
    VALIDATING = "validating"
    COMMITTING = "committing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})
_SEQUENCE = [TaskState.QUEUED, TaskState.PARSING, TaskState.CHECKING, TaskState.WRITING,
             TaskState.POSTPROCESSING, TaskState.VALIDATING, TaskState.COMMITTING, TaskState.SUCCEEDED]


@dataclass(frozen=True)
class ProgressEvent:
    task_id: str
    state: TaskState
    monotonic_time: float


class TaskContext:
    """One lock arbitrates cancellation against the noncancellable commit boundary."""
    def __init__(self, timeout=60.0):
        if not 0 < timeout <= 3600:
            raise ValueError("Invalid task timeout")
        self.task_id = str(uuid.uuid4())
        self.deadline = time.monotonic() + timeout
        self.cancel_event = threading.Event()
        self.work_dir: Path | None = None
        self.owner: str | None = None
        self._lock = threading.RLock()
        self._state = TaskState.QUEUED
        self._issues = ()
        self._events = deque([ProgressEvent(self.task_id, self._state, time.monotonic())], maxlen=16)

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def issues(self):
        with self._lock:
            return self._issues

    def record_issues(self, issues):
        with self._lock:
            known = set(self._issues)
            added = []
            for issue in issues:
                if issue not in known:
                    known.add(issue)
                    added.append(issue)
            self._issues += tuple(added)

    def drain_events(self):
        """GUI polls on its own thread; no worker callback can mutate a widget."""
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            return events

    def _set(self, state):
        self._state = state
        self._events.append(ProgressEvent(self.task_id, state, time.monotonic()))

    def request_cancel(self):
        with self._lock:
            if self._state in TERMINAL or self._state == TaskState.COMMITTING:
                return False
            if self._state != TaskState.CANCELLING:
                self.cancel_event.set()
                self._set(TaskState.CANCELLING)
            return True

    def check(self):
        from ..conversion.common import check_budget
        check_budget(self.deadline, self.cancel_event)

    def advance(self, state):
        state = TaskState(state)
        with self._lock:
            self.check()
            if state not in _SEQUENCE or self._state not in _SEQUENCE:
                raise RuntimeError("Illegal task transition")
            if _SEQUENCE.index(state) != _SEQUENCE.index(self._state) + 1:
                raise RuntimeError("Illegal task transition")
            self._set(state)

    def begin_retry(self):
        with self._lock:
            self.check()
            if self._state != TaskState.QUEUED:
                raise RuntimeError("Illegal retry transition")
            self._set(TaskState.VALIDATING)

    def finish(self, state):
        state = TaskState(state)
        with self._lock:
            if self._state in TERMINAL:
                raise RuntimeError("Duplicate terminal transition")
            if state not in TERMINAL:
                raise RuntimeError("Expected terminal state")
            if state == TaskState.SUCCEEDED and self._state != TaskState.COMMITTING:
                raise RuntimeError("Success before commit")
            if state == TaskState.CANCELLED and self._state == TaskState.COMMITTING:
                raise RuntimeError("Commit cannot be cancelled")
            self._set(state)


@dataclass(frozen=True)
class ExportResult:
    task_id: str
    snapshot: ExportSnapshot
    state: TaskState
    output_path: Path | None = None
    sha256: str | None = None
    issues: tuple[Issue, ...] = ()
    recovery_id: str | None = None
    elapsed_seconds: float = 0.0
    failure: Issue | None = None

