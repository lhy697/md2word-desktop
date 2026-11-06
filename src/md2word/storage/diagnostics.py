"""Only fixed codes and numeric metadata reach rotating logs or default bundles."""
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import platform
import re
import zipfile

from PySide6.QtCore import qVersion
from .. import __version__
from ..domain.models import failure


STAGES = {"startup", "storage", "preview", "export", "shutdown", "diagnostics"}


def safe_event(stage, code, elapsed_ms=0):
    if stage not in STAGES or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        raise ValueError("Only stage and stable codes are loggable")
    return {"time":datetime.datetime.now(datetime.timezone.utc).isoformat(), "stage":stage,
            "code":code, "elapsed_ms":max(0, min(int(elapsed_ms), 86400000)), "version":__version__}


class Diagnostics:
    MAX_BYTES = 256 * 1024
    BACKUPS = 3

    def __init__(self, directory):
        self.directory = directory
        self.handler = None
        self.failed = False
        try:
            self.handler = RotatingFileHandler(directory / "events.jsonl", maxBytes=self.MAX_BYTES,
                                               backupCount=self.BACKUPS, encoding="utf-8")
            self.handler.setFormatter(logging.Formatter("%(message)s"))
        except OSError:
            self.failed = True

    def record(self, stage, code, elapsed_ms=0):
        event = safe_event(stage, code, elapsed_ms)
        if self.handler is not None:
            try:
                if self.handler.shouldRollover(logging.makeLogRecord({"msg":json.dumps(event)})):
                    self.handler.doRollover()
                # Direct writes avoid logging.handleError printing sensitive traceback paths.
                self.handler.stream.write(json.dumps(event, ensure_ascii=True) + "\n")
                self.handler.flush()
            except (OSError, ValueError):
                self.failed = True

    def events(self):
        result = []
        for suffix in (".3", ".2", ".1", ""):
            path = self.directory / ("events.jsonl" + suffix)
            try:
                with path.open("rb") as stream:
                    data = stream.read(self.MAX_BYTES + 2048)
                for line in data.decode("utf-8", errors="replace").splitlines():
                    try:
                        raw = json.loads(line)
                        # Reconstruct allowed fields. Never copy a raw user-edited log line.
                        event = safe_event(raw["stage"], raw["code"], raw["elapsed_ms"])
                        timestamp = datetime.datetime.fromisoformat(raw["time"])
                        if timestamp.utcoffset() is None:
                            continue
                        event["time"] = timestamp.isoformat()
                        result.append(event)
                    except (ValueError, KeyError, TypeError, OverflowError):
                        continue
            except OSError:
                continue
        return result

    def bundle(self, target, *, session=None):
        try:
            # Exclusive creation prevents quietly overwriting a user's existing archive.
            with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                summary = {"schema_version":1, "application_version":__version__, "python":platform.python_version(),
                           "qt":qVersion(), "platform":platform.system(), "content_included":session is not None,
                           "uploaded":False}
                archive.writestr("summary.json", json.dumps(summary, indent=2))
                archive.writestr("events.jsonl", "".join(json.dumps(e) + "\n" for e in self.events()))
                if session is not None:
                    archive.writestr("content/original.txt", session.original_text)
                    archive.writestr("content/current.txt", session.current_text)
        except (OSError, ValueError, RuntimeError):
            raise failure("DIAGNOSTIC_SAVE_FAILED", "诊断包未能保存。", "选择新的文件名并重试。", "storage") from None

    def close(self):
        if self.handler:
            self.handler.close()
