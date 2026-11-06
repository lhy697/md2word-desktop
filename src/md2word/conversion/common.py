from pathlib import Path
import json
import threading
import time
from ..domain.models import failure

def check_budget(deadline: float, cancel: threading.Event | None):
    if cancel is not None and cancel.is_set():
        raise failure("EXPORT_CANCELLED", "转换已取消，原文仍保留。",
                      "需要时重新转换。", "export")
    if time.monotonic() >= deadline:
        raise failure("EXPORT_TIMEOUT", "转换超过总时间限制，未提交文件。",
                      "拆分资料或重试。", "export")

def read_log(path: Path) -> list[dict]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Oversized writer log")
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            raise ValueError("Invalid writer log")
        return value
    except (OSError, ValueError):
        raise failure("WRITER_LOG_INVALID", "转换诊断无法可靠读取。",
                      "停止写出并重试。", "export") from None

def remove_log(path: Path):
    path.unlink(missing_ok=True)

def conversion_messages(logs, reference: Path):
    # Pandoc reports the controlled reference as an INFO resource load.
    return [item for item in logs if not (
        item.get("type") == "LoadedResource" and item.get("verbosity") == "INFO"
        and item.get("for") == str(reference) and item.get("from") == str(reference))]
