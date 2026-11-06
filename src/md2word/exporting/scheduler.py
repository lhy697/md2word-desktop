"""One export worker, zero waiting exports. UI polls immutable events/results."""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import threading

from ..domain.models import failure
from .models import TaskContext
from .service import ExportService


@dataclass(frozen=True)
class TaskHandle:
    context: TaskContext
    _future: object

    @property
    def done(self):
        return self._future.done()

    def cancel(self):
        return self.context.request_cancel()

    def poll(self):
        """Nonblocking result retrieval for GUI."""
        return self._future.result() if self.done else None

    def result(self, timeout=None):
        """Blocking adapter for CLI/tests only, never call from GUI main thread."""
        return self._future.result(timeout)


class Scheduler:
    EXPORT_CONCURRENCY = 1
    PREVIEW_CONCURRENCY = 1
    PREVIEW_MAX_PENDING = 1  # Stage 06 must implement a separate latest-only lane.

    def __init__(self, service=None):
        self.service = service or ExportService()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="md2word-export")
        self._lock = threading.Lock()
        self._active = None
        self._closed = False

    def submit(self, snapshot, intent, *, timeout=60.0, recovery_id=None):
        with self._lock:
            if self._closed:
                raise failure("SCHEDULER_CLOSED", "导出调度器已关闭。", "重新打开应用。", "export")
            if self._active is not None and not self._active.done:
                raise failure("EXPORT_BUSY", "已有一个导出任务正在处理。", "等待完成或先取消当前任务。", "export")
            context = TaskContext(timeout)
            future = self._pool.submit(self.service.run, snapshot, intent, context, recovery_id=recovery_id)
            self._active = TaskHandle(context, future)
            return self._active

    def retry(self, result, intent, *, timeout=60.0):
        if result.recovery_id is None:
            raise failure("RECOVERY_UNAVAILABLE", "没有可重试的已验证成品。", "重新转换原文。", "export")
        return self.submit(result.snapshot, intent, timeout=timeout, recovery_id=result.recovery_id)

    def request_shutdown(self):
        """Nonblocking close protocol: reject new work, cancel before commit, poll done."""
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active.cancel()
            self._pool.shutdown(wait=False, cancel_futures=False)

    @property
    def idle(self):
        with self._lock:
            return self._active is None or self._active.done

    def dispose(self):
        """Call after idle; no blocking GUI close and no deleting active task files."""
        with self._lock:
            if self._active is not None and not self._active.done:
                raise failure("EXPORT_BUSY", "后台任务尚未结束。", "等待终态后释放资源。", "export")
            self._closed = True
            self._pool.shutdown(wait=False, cancel_futures=False)
        self.service.close()

