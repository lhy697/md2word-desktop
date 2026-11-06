"""Bounded JSON reads and same-directory atomic writes, shared with styles."""
import json
import os
from pathlib import Path
import uuid

from ..domain.models import failure
from ..resources import ResourceLocator


class UserPaths:
    def __init__(self, root=None):
        try:
            self.root = Path(root or ResourceLocator.data_root()).resolve()
            for name in ("settings", "styles", "drafts", "logs"):
                path = self.root / name
                path.mkdir(parents=True, exist_ok=True)
                setattr(self, name, path)
            if root is None:
                from PySide6.QtCore import QStandardPaths
                self.tasks = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)) / "md2word-export"
            else:
                self.tasks = self.root / "tasks"
            self.tasks.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise failure("USER_DATA_UNAVAILABLE", "用户数据目录无法使用。", "检查用户目录权限后重试。", "storage") from None


def read_json(path, limit):
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise OSError("Unsafe state file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON size limit")
    try:
        return json.loads(data.decode("utf-8")), data
    except RecursionError:
        # Treat excessive decoder nesting like other invalid JSON so repository
        # load/save boundaries can recover backups and preserve damaged bytes.
        raise ValueError("JSON nesting limit") from None


def atomic_bytes(path, data, *, replace=True):
    """Only the final, closed temp is published; never overwrite through symlinks."""
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise OSError("Unsafe state file")
    temp = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    created = False
    try:
        with temp.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    finally:
        if created:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                # Exact owned temp only; never replace the original write failure.
                pass


def json_bytes(raw):
    return (json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
