"""Recover valid backups; never silently replace damaged or future state."""
from dataclasses import dataclass
import re
import uuid

from ..domain.models import DocumentError, Issue, Severity, failure
from .files import atomic_bytes, json_bytes, read_json
from .models import Draft, Settings


@dataclass(frozen=True)
class LoadedState:
    value: object = None
    source: str = ""
    issues: tuple = ()


class StateRepository:
    version = 1
    model = None
    limit = 0
    prefix = "STATE"

    def __init__(self, directory, name):
        self.directory = directory
        self.path = directory / (name + ".json")
        self.backup = directory / (name + ".backup.json")
        self.blocked = False

    def problem(self, suffix, message):
        return failure(self.prefix + "_" + suffix, message, "可重试；已有文件会保留。清空草稿需主动确认。", "storage")

    def parse(self, raw):
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            raise ValueError("Missing schema")
        if raw["schema_version"] > self.version or raw["schema_version"] < 0:
            raise self.problem("INCOMPATIBLE", "本地数据版本不兼容，已停止写回原文件。")
        if raw["schema_version"] == 0:
            raw = self.migrate(raw)
        if self.version == 2 and raw["schema_version"] == 1:
            raw = dict(raw, schema_version=2)
        return self.model.model_validate(raw)

    def migrate(self, raw):
        raise ValueError("Unknown legacy schema")

    def _read(self, path):
        raw, data = read_json(path, self.limit)
        return self.parse(raw), data, raw

    def load(self):
        issues = []
        for path, source in ((self.path, "当前草稿"), (self.backup, "上一次有效快照")):
            try:
                value, _, _ = self._read(path)
                # A future backup is protected too, even with a readable primary.
                other = self.backup if path == self.path else None
                if other and other.exists():
                    try:
                        self._read(other)
                    except DocumentError:
                        self.blocked = True
                        raise
                    except (OSError, ValueError, TypeError):
                        pass
                self.blocked = False
                return LoadedState(value, source, tuple(issues))
            except FileNotFoundError:
                continue
            except DocumentError as exc:
                self.blocked = True
                return LoadedState(issues=tuple(issues) + (exc.issue,))
            except (OSError, ValueError, TypeError):
                issues.append(Issue(self.prefix + "_READ_FAILED", Severity.WARNING, "storage",
                                    "本地数据无法读取，保留文件并尝试有效备份。", "恢复有效备份或主动清空草稿。"))
        self.blocked = bool(issues)
        return LoadedState(issues=tuple(issues))

    def _preserve(self, path, data, suffix):
        # At most four damaged copies; never delete potentially useful evidence.
        copies = tuple(self.directory.glob(self.path.stem + ".preserved-*.json"))
        if len(copies) >= 4:
            raise self.problem("PRESERVE_LIMIT", "保留的损坏数据已达上限，请先备份整理后重试。")
        target = self.directory / (self.path.stem + ".preserved-" + suffix + "-" + uuid.uuid4().hex + ".json")
        atomic_bytes(target, data, replace=False)

    def save(self, value):
        if self.blocked:
            raise self.problem("PROTECTED", "已有损坏或不兼容数据未处理，未覆盖本地文件。")
        try:
            raw = value.model_dump(mode="json")
            data = json_bytes(raw)
            self.model.model_validate_json(data)
            if len(data) > self.limit:
                raise ValueError("State size limit")
            previous = None
            for path in (self.path, self.backup):
                try:
                    _, old, old_raw = self._read(path)
                    if old_raw["schema_version"] < self.version:
                        migration = self.directory / (path.stem + f'.v{old_raw["schema_version"]}.json')
                        if not migration.exists():
                            atomic_bytes(migration, old, replace=False)
                    if path == self.path:
                        previous = old
                except FileNotFoundError:
                    continue
                except DocumentError:
                    self.blocked = True
                    raise
                except (ValueError, TypeError):
                    _, old = read_json_bytes(path, self.limit)
                    self._preserve(path, old, "damaged")
            if previous is not None:
                atomic_bytes(self.backup, previous)
            atomic_bytes(self.path, data)
        except DocumentError:
            raise
        except (OSError, ValueError, TypeError):
            raise self.problem("SAVE_FAILED", "本地保存失败，当前内存内容仍保留。") from None
        return value


def read_json_bytes(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OSError("Cannot preserve oversized state")
    return None, data


class DraftRepository(StateRepository):
    version = 2
    model, limit, prefix = Draft, 24 * 1024 * 1024, "DRAFT"

    def __init__(self, directory):
        super().__init__(directory, "session")

    def migrate(self, raw):
        allowed = {"schema_version", "session_id", "original_text", "current_text", "mode",
                   "revision", "style_snapshot", "saved_at", "title"}
        if set(raw) != allowed:
            raise ValueError("Unknown v0 draft")
        result = dict(raw)
        result.update(schema_version=1, content_revision=result.pop("revision"), style_revision=0, dirty=True)
        return result

    def clear(self):
        try:
            # Remove backup first; report failure before claiming that a clear succeeded.
            copies = list(self.directory.glob("session.preserved-*.json"))
            copies += list(self.directory.glob("session*.v0.json"))
            copies += list(self.directory.glob("session*.v1.json"))
            paths = [self.backup, *copies, self.path]
            for path in paths:
                if path.is_symlink() or path.resolve().parent != self.directory.resolve():
                    raise OSError("Unsafe draft")
                path.unlink(missing_ok=True)
        except OSError:
            raise self.problem("CLEAR_FAILED", "草稿尚未完全清空，请重试；当前编辑内容未改动。") from None
        self.blocked = False

    def cleanup_temps(self):
        try:
            for path in self.directory.glob(".session*.tmp"):
                if re.fullmatch(r"\.session(?:\.backup)?\.json\.[0-9a-f]{32}\.tmp", path.name):
                    if not path.is_symlink() and path.resolve().parent == self.directory.resolve():
                        path.unlink(missing_ok=True)
        except OSError:
            raise self.problem("TEMP_CLEANUP_FAILED", "上次未完成的草稿临时文件尚未清理。") from None


class SettingsRepository(StateRepository):
    model, limit, prefix = Settings, 64 * 1024, "SETTINGS"

    def __init__(self, directory):
        super().__init__(directory, "preferences")

    def migrate(self, raw):
        if set(raw) != {"schema_version", "geometry", "autosave", "last_directory", "motion"}:
            raise ValueError("Unknown v0 settings")
        geometry = raw["geometry"]
        if not isinstance(geometry, list) or len(geometry) != 4:
            raise ValueError("Invalid old geometry")
        return dict(schema_version=1, x=geometry[0], y=geometry[1], width=geometry[2], height=geometry[3],
                    auto_save=raw["autosave"], last_directory=raw["last_directory"], motion=raw["motion"])
