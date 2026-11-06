"""Strict local input. No UI, extension-based mode guessing or file writes."""
from pathlib import Path
from .domain.models import DocumentSession, InputMode, failure

MAX_INPUT_BYTES = 2 * 1024 * 1024

def check_text(text: str, max_bytes: int = MAX_INPUT_BYTES):
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise failure("INPUT_ENCODING", "输入包含无法编码的 Unicode 字符。",
                      "重新复制原文或另存为 UTF-8。", "import") from None
    if len(encoded) > max_bytes:
        raise failure("INPUT_TOO_LARGE", "输入超过当前大小上限，未截断内容。",
                      "将资料拆分为较小文档后重试。", "import")
    if any(ord(c) < 32 and c not in "\t\r\n" or ord(c) in (0xFFFE, 0xFFFF) for c in text):
        raise failure("INPUT_CONTROL_CHARACTER", "输入含有 Word 无法保留的控制字符。",
                      "检查原文件中的控制字符；程序不会自动删除。", "import")

def read_input(path: Path, mode: InputMode) -> DocumentSession:
    path = Path(path)
    if path.suffix.lower() not in {".md", ".txt"}:
        raise failure("INPUT_TYPE", "当前只支持 .md 和 .txt 文件。",
                      "将资料另存为 UTF-8 文本文件。", "import")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise failure("INPUT_READ_FAILED", "无法读取所选文件。",
                      "检查文件是否存在及访问权限。", "import") from None
    if len(data) > MAX_INPUT_BYTES:
        raise failure("INPUT_TOO_LARGE", "输入超过当前大小上限，未截断内容。",
                      "将资料拆分为较小文档后重试。", "import")
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise failure("INPUT_ENCODING", "文件不是有效的 UTF-8 文本。",
                      "请另存为 UTF-8 或带 BOM 的 UTF-8 后重试。", "import") from None
    check_text(text)
    return DocumentSession.create(text, InputMode(mode))
