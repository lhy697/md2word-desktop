"""Cleanup only verified, directly owned task directories; never follow reparse points."""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from ..domain.models import failure


def reject_reparse(path: Path):
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise failure("UNSAFE_PATH", "路径包含符号链接或重解析点。", "选择普通本地目录。", "export")


def create_task(root: Path):
    root = Path(os.path.abspath(root))
    reject_reparse(root)
    root.mkdir(parents=True, exist_ok=True)
    reject_reparse(root)
    task = Path(tempfile.mkdtemp(prefix="export-", dir=root))
    owner = str(uuid.uuid4())
    (task / ".md2word-task.json").write_text(json.dumps({"owner": owner, "stage": "05"}), "utf-8")
    return task, owner


def verify_task(task: Path, root: Path, owner: str):
    reject_reparse(task)
    if task.parent.resolve() != root.resolve():
        raise ValueError("Task escaped its root")
    if json.loads((task / ".md2word-task.json").read_text("utf-8")) != {"owner": owner, "stage": "05"}:
        raise ValueError("Task ownership changed")
    for current, directories, files in os.walk(task, followlinks=False):
        for name in [*directories, *files]:
            reject_reparse(Path(current) / name)


def cleanup_task(task: Path, root: Path, owner: str) -> bool:
    try:
        reject_reparse(task)
        if task.parent.resolve() != root.resolve():
            return False
        try:
            identity = task.stat().st_dev, task.stat().st_ino
        except FileNotFoundError:
            return True  # A previously verified task may already have been removed.
        verify_task(task, root, owner)
        marker = task / ".md2word-task.json"
        # Never let a partially failed recursive delete remove our credentials.
        for item in task.iterdir():
            if item == marker:
                continue
            reject_reparse(item)
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        verify_task(task, root, owner)
        marker.unlink()
        try:
            task.rmdir()
        except OSError:
            # A directory handle can block the final rmdir after all files were
            # removed. Restore the marker only on the same verified directory.
            reject_reparse(task)
            current = task.stat()
            if ((current.st_dev, current.st_ino) == identity
                    and task.parent.resolve() == root.resolve() and not marker.exists()):
                with marker.open("x", encoding="utf-8") as stream:
                    json.dump({"owner": owner, "stage": "05"}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            raise
        return True
    except Exception:
        # Refuse cleanup on an ownership/path verification failure, never guess.
        return False

