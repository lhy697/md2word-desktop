"""A single writer, one replaceable pending snapshot, monotonic request tickets."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time

from ..domain.models import DocumentError, Issue, Severity


@dataclass(frozen=True)
class SaveResult:
    ticket: int
    operation: str
    value: object = None
    issue: object = None
    elapsed_ms: int = 0


class DraftLane:
    def __init__(self, repository):
        self.repository = repository
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="md2word-draft")
        self.active = None
        self.pending = None
        self.latest_ticket = 0
        self.last_saved = 0
        self.closed = False

    def request(self, value=None, *, clear=False):
        if self.closed:
            raise RuntimeError("Draft lane closed")
        self.latest_ticket += 1
        request = (self.latest_ticket, "clear" if clear else "save", value)
        if self.active is None:
            self._start(request)
        else:
            self.pending = request
        return request[0]

    def _start(self, request):
        self.active = self.pool.submit(self._write, request)

    def _write(self, request):
        ticket, operation, value = request
        started = time.monotonic()
        try:
            if operation == "clear":
                self.repository.clear()
            else:
                self.repository.save(value)
            return SaveResult(ticket, operation, value, elapsed_ms=round((time.monotonic() - started) * 1000))
        except DocumentError as exc:
            return SaveResult(ticket, operation, issue=exc.issue, elapsed_ms=round((time.monotonic() - started) * 1000))
        except Exception:
            return SaveResult(ticket, operation, issue=Issue("DRAFT_SAVE_FAILED", Severity.WARNING,
                "storage", "草稿未保存，内存内容仍保留。", "重试保存；关闭前保留原文。"))

    def poll(self):
        if self.active is None or not self.active.done():
            return None
        result = self.active.result()
        self.active = None
        if result.issue is None:
            self.last_saved = result.ticket
        if self.pending is not None:
            request, self.pending = self.pending, None
            self._start(request)
        return result

    def pause(self):
        # Pausing auto-save must not cancel an explicit, already-confirmed clear.
        if self.pending is not None and self.pending[1] == "save":
            self.pending = None

    @property
    def idle(self):
        return self.active is None and self.pending is None

    def close(self):
        if not self.idle:
            raise RuntimeError("Cannot release active writer")
        self.closed = True
        self.pool.shutdown(wait=False)
