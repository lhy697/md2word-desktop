"""Independent cleanup leases with bounded, path-free diagnostics and safe retry."""
from collections import deque
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

from ..domain.models import Issue, Severity
from .workspace import cleanup_task, reject_reparse


class CleanupTracker:
    JOURNAL = ".cleanup-pending"

    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        self._pending = {}
        self._diagnostics = deque(maxlen=64)
        self._lock = threading.RLock()
        self._load()

    def _warning(self, code, owner=None):
        message, action = {
            "CLEANUP_PENDING": ("部分临时文件暂未清理，已保留后续清理记录。",
                                "关闭占用程序后重试清理；原文件不受影响。"),
            "CLEANUP_TRACKING_FAILED": ("临时清理记录未能完整保存或读取。",
                                        "恢复临时目录访问权限后重试清理。"),
        }[code]
        issue = Issue(code, Severity.WARNING, "cleanup", message, action, diagnostic_ref=owner)
        self._diagnostics.append(issue)
        return issue

    @property
    def pending_count(self):
        with self._lock:
            return len(self._pending)

    def drain_diagnostics(self):
        with self._lock:
            values = tuple(self._diagnostics)
            self._diagnostics.clear()
            return values

    def _record(self, task, owner):
        if task.parent != self.root or not re.fullmatch(r"export-[A-Za-z0-9_-]{6,64}", task.name):
            raise ValueError("Not an owned task name")
        if str(uuid.UUID(owner)) != owner:
            raise ValueError("Invalid owner")
        return {"schema": 1, "stage": "05", "task": task.name, "owner": owner}

    def _read(self, path):
        reject_reparse(path)
        with path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("Oversized cleanup record")
        value = json.loads(raw)
        task = self.root / value["task"]
        expected = self._record(task, value["owner"])
        if value != expected or type(value["schema"]) is not int or path.name != task.name + ".json":
            raise ValueError("Invalid cleanup record")
        return task, value["owner"]

    def _load(self):
        journal = self.root / self.JOURNAL
        complete = True
        try:
            reject_reparse(journal)
            if not journal.exists():
                return True
            # Stream the entire journal. A limit on directory entries silently
            # loses valid leases, including when non-record files use the quota.
            with os.scandir(journal) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if path.suffix != ".json":
                        continue
                    try:
                        task, owner = self._read(path)
                        known = self._pending.get(task.name)
                        if known is not None and known != (task, owner):
                            raise ValueError("Cleanup record ownership changed")
                        self._pending[task.name] = (task, owner)
                    except Exception:
                        complete = False
                        self._warning("CLEANUP_TRACKING_FAILED")
        except Exception:
            complete = False
            self._warning("CLEANUP_TRACKING_FAILED")
        return complete

    def _save(self, task, owner):
        value = self._record(task, owner)
        journal = self.root / self.JOURNAL
        reject_reparse(journal)
        journal.mkdir(exist_ok=True)
        reject_reparse(journal)
        target = journal / (task.name + ".json")
        if target.exists():
            if self._read(target) != (task, owner):
                raise ValueError("Cleanup record ownership changed")
            return
        fd, name = tempfile.mkstemp(prefix=".record-", suffix=".tmp", dir=journal)
        staged = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if os.name == "nt":
                    os.rename(staged, target)
                else:
                    os.link(staged, target)
            except FileExistsError:
                if self._read(target) != (task, owner):
                    raise ValueError("Cleanup record ownership changed")
        finally:
            staged.unlink(missing_ok=True)

    def _forget(self, task, owner):
        journal = self.root / self.JOURNAL
        target = journal / (task.name + ".json")
        reject_reparse(journal)
        if target.exists():
            if self._read(target) != (task, owner):
                raise ValueError("Cleanup record ownership changed")
            target.unlink()
        self._pending.pop(task.name, None)
        try:
            journal.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Other records or a temporarily locked directory remain untouched.
            pass

    def attempt(self, task, owner):
        task = Path(os.path.abspath(task))
        with self._lock:
            issues = []
            if cleanup_task(task, self.root, owner):
                try:
                    self._forget(task, owner)
                except Exception:
                    self._pending[task.name] = (task, owner)
                    issues.append(self._warning("CLEANUP_TRACKING_FAILED", owner))
                return True, tuple(issues)
            self._pending[task.name] = (task, owner)
            issues.append(self._warning("CLEANUP_PENDING", owner))
            try:
                self._save(task, owner)
            except Exception:
                issues.append(self._warning("CLEANUP_TRACKING_FAILED", owner))
            return False, tuple(issues)

    def retry(self):
        with self._lock:
            complete = self._load()
            for task, owner in tuple(self._pending.values()):
                self.attempt(task, owner)
            if complete and not self._pending:
                # A different instance may have published another lease while
                # this pass was deleting files; leave it visible for the next pass.
                complete = self._load()
            return complete and not self._pending

