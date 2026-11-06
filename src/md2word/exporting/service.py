"""Service orchestration with no Qt/widget dependency and no mutable session access."""
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
import threading
import time
import uuid

from ..conversion.math_check import MathChecker
from ..conversion.postprocess import postprocess
from ..conversion.validation import validate_docx
from ..conversion.writer import DocxWriter
from ..domain.models import DocumentError, Severity, failure
from ..domain.normalizer import normalize
from ..parsing import Parser
from ..templates.fonts import check_fonts
from .models import ExportResult, ExportSnapshot, TaskContext, TaskState
from .output import OutputCommitter, fingerprint, output_error
from .workspace import create_task, verify_task
from .cleanup import CleanupTracker


@dataclass(frozen=True)
class RetainedArtifact:
    recovery_id: str
    snapshot: ExportSnapshot
    task: Path
    owner: str
    path: Path
    sha256: str
    checked: object
    expires_at: float


class ExportService:
    RETAIN_SECONDS = 15 * 60
    MAX_RETAINED = 3

    def __init__(self, *, work_root=None, committer=None):
        self.work_root = Path(work_root or Path(tempfile.gettempdir()) / "md2word-export")
        self.committer = committer or OutputCommitter()
        self._retained = OrderedDict()
        self._expiry_timers = {}
        self._retained_lock = threading.RLock()
        self._cleanup = CleanupTracker(self.work_root)

    @property
    def pending_cleanup_count(self):
        return self._cleanup.pending_count

    def drain_diagnostics(self):
        return self._cleanup.drain_diagnostics()

    def retry_cleanup(self):
        return self._cleanup.retry()

    def _retain(self, artifact):
        with self._retained_lock:
            self.expire_retained()
            artifact = replace(artifact, expires_at=time.monotonic() + self.RETAIN_SECONDS)
            self._retained[artifact.recovery_id] = artifact
            old_timer = self._expiry_timers.pop(artifact.recovery_id, None)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(self.RETAIN_SECONDS, self.expire_retained)
            timer.daemon = True
            self._expiry_timers[artifact.recovery_id] = timer
            timer.start()
            while len(self._retained) > self.MAX_RETAINED:
                key, old = self._retained.popitem(last=False)
                self._expiry_timers.pop(key).cancel()
                self._cleanup.attempt(old.task, old.owner)
        return artifact.recovery_id

    def expire_retained(self):
        with self._retained_lock:
            expired = [key for key, value in self._retained.items() if time.monotonic() >= value.expires_at]
            for key in expired:
                old = self._retained.pop(key)
                timer = self._expiry_timers.pop(key, None)
                if timer is not None:
                    timer.cancel()
                self._cleanup.attempt(old.task, old.owner)

    def discard(self, recovery_id):
        with self._retained_lock:
            artifact = self._retained.pop(recovery_id, None)
            timer = self._expiry_timers.pop(recovery_id, None)
            if timer is not None:
                timer.cancel()
        return artifact is not None and self._cleanup.attempt(artifact.task, artifact.owner)[0]

    def close(self):
        with self._retained_lock:
            keys = tuple(self._retained)
        for key in keys:
            self.discard(key)
        self.retry_cleanup()

    def _build(self, snapshot, context):
        parser = Parser(profile=snapshot.reader)
        runner = parser.runner
        context.advance(TaskState.PARSING)
        parsed = parser.parse(snapshot.input, cwd=context.work_dir,
                              deadline=context.deadline, cancel=context.cancel_event)
        context.record_issues(parsed.issues)
        context.check()
        context.advance(TaskState.CHECKING)
        document = normalize(parsed, deadline=context.deadline, cancel=context.cancel_event)
        context.record_issues(document.issues)
        context.check()
        document.require_exportable()
        from ..domain.local_formatting import resolve_formats
        _,local_formats,unresolved,local_issues = resolve_formats(document,snapshot.local_formats)
        if unresolved:
            context.record_issues(local_issues)
            raise DocumentError(local_issues[0])
        checked = MathChecker(runner).check_math(
            document, work_dir=context.work_dir, reference_key="lecture",
            deadline=context.deadline, cancel=context.cancel_event)
        context.record_issues(checked.issues)
        checked.require_ready()
        context.check()
        context.advance(TaskState.WRITING)
        target, _ = DocxWriter(runner).write_docx(
            checked, work_dir=context.work_dir, deadline=context.deadline, cancel=context.cancel_event)
        context.advance(TaskState.POSTPROCESSING)
        styled = postprocess(target, context.work_dir / "formatted.docx", checked, snapshot.style,
                             deadline=context.deadline, cancel=context.cancel_event,local_formats=local_formats)
        context.record_issues(styled.issues)
        fonts = check_fonts(snapshot.style)
        context.record_issues(fonts.issues)
        from ..templates.fonts import check_local_fonts
        local_fonts = check_local_fonts(local_formats)
        context.record_issues(local_fonts)
        checked = replace(checked, issues=checked.issues + styled.issues + fonts.issues + local_fonts)
        context.advance(TaskState.VALIDATING)
        self._validate(styled.path, checked, context)
        value = fingerprint(styled.path, context.check).sha256
        return RetainedArtifact(str(uuid.uuid4()), snapshot, context.work_dir, context.owner,
                                styled.path, value, checked, time.monotonic() + self.RETAIN_SECONDS)

    def _validate(self, path, checked, context):
        context.check()
        result = validate_docx(path, checked, deadline=context.deadline, cancel=context.cancel_event)
        context.record_issues(result.issues)
        if not result.passed:
            if result.issues:
                raise DocumentError(result.issues[0])
            raise failure("DOCX_INVALID", "成品验证未通过。", "重新转换原文。", "validate")
        context.check()

    def _load_retry(self, recovery_id, snapshot, context):
        with self._retained_lock:
            self.expire_retained()
            artifact = self._retained.get(recovery_id)
            if artifact is not None and artifact.snapshot == snapshot:
                # Consume the recovery lease before validation so an expiry timer
                # cannot remove the artifact while this worker is using it.
                del self._retained[recovery_id]
                timer = self._expiry_timers.pop(recovery_id, None)
                if timer is not None:
                    timer.cancel()
        if artifact is None or artifact.snapshot != snapshot:
            raise failure("RECOVERY_EXPIRED", "暂存结果已过期或不属于此快照。", "重新转换仍保留的原文。", "export")
        context.work_dir, context.owner = artifact.task, artifact.owner
        context.record_issues(artifact.checked.issues)
        verify_task(artifact.task, self.work_root, artifact.owner)
        context.begin_retry()
        try:
            actual_sha = fingerprint(artifact.path, context.check).sha256
        except FileNotFoundError:
            raise failure("ARTIFACT_CHANGED", "暂存成品已不存在，停止重试。", "重新转换仍保留的原文。", "validate") from None
        if actual_sha != artifact.sha256:
            self.discard(recovery_id)
            raise failure("ARTIFACT_CHANGED", "暂存成品已被修改，停止重试。", "重新转换原文。", "validate")
        self._validate(artifact.path, artifact.checked, context)
        return artifact

    def run(self, snapshot: ExportSnapshot, intent, context: TaskContext, *, recovery_id=None):
        started = time.monotonic()
        artifact = None
        retain = False
        original_issues = ()
        result = None
        try:
            context.check()
            self.retry_cleanup()
            context.check()
            if recovery_id is None:
                context.work_dir, context.owner = create_task(self.work_root)
                artifact = self._build(snapshot, context)
            else:
                artifact = self._load_retry(recovery_id, snapshot, context)
            original_issues = artifact.checked.issues
            target = self.committer.commit(artifact.path, artifact.sha256, intent, context)
            context.finish(TaskState.SUCCEEDED)
            result = ExportResult(context.task_id, snapshot, TaskState.SUCCEEDED, target,
                                  artifact.sha256, original_issues, elapsed_seconds=time.monotonic() - started)
        except Exception as exc:
            original_issues = context.issues
            if isinstance(exc, DocumentError):
                issue = exc.issue
            elif isinstance(exc, OSError):
                issue = output_error(exc).issue
            else:
                issue = failure("EXPORT_FAILED", "导出未完成，原文仍保留。", "重试或反馈问题代码。", "export").issue
            if issue.code == "PARSE_TIMEOUT":
                issue = failure("EXPORT_TIMEOUT", "导出超过总时间限制，未提交文件。", "拆分资料或重试。", "export").issue
            cancelled = context.cancel_event.is_set() and context.state != TaskState.COMMITTING
            state = TaskState.CANCELLED if cancelled else TaskState.FAILED
            if cancelled:
                issue = failure("EXPORT_CANCELLED", "导出已取消，未提交文件。", "需要时重新导出。", "export").issue
            # Never keep an unvalidated, modified, cancelled or timed-out artifact.
            if (artifact is not None and not cancelled and issue.code.startswith("OUTPUT_")):
                retain = True
                recovery_id = self._retain(artifact)
            else:
                recovery_id = None
            context.finish(state)
            if issue.code not in {"DOCUMENT_BLOCKED", "MATH_UNCERTAIN"} or not any(
                    i.severity == Severity.BLOCKING for i in original_issues):
                context.record_issues((issue,))
            result = ExportResult(context.task_id, snapshot, state, issues=context.issues,
                                  recovery_id=recovery_id, elapsed_seconds=time.monotonic() - started,
                                  failure=issue)
        finally:
            if not retain and context.work_dir is not None and context.owner is not None:
                with self._retained_lock:
                    for key, value in tuple(self._retained.items()):
                        if value.task == context.work_dir:
                            del self._retained[key]
                            timer = self._expiry_timers.pop(key, None)
                            if timer is not None:
                                timer.cancel()
                _, cleanup_issues = self._cleanup.attempt(context.work_dir, context.owner)
                if result is not None and cleanup_issues:
                    result = replace(result, issues=result.issues + cleanup_issues)
        return result

