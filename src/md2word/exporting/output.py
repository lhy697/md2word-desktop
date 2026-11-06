"""No-clobber publication; existing-target replacement fails closed on this platform."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from ..domain.models import failure
from .workspace import reject_reparse


def candidate_filename(heading: str | None) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", heading or "").strip().rstrip(". ")
    name = name[:100].rstrip(". ")
    if not name:
        name = "未命名文档"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name):
        name = "_" + name
    if name.lower().endswith(".docx"):
        name = name[:-5].rstrip(". ") or "未命名文档"
    return name + ".docx"


def candidate_from_document(document) -> str:
    """Suggest from the first parsed heading; never changes the selected output path."""
    from ..domain.ast_index import at_path
    def inline_text(value):
        if isinstance(value, list):
            return ''.join(inline_text(x) for x in value)
        if not isinstance(value, dict):
            return ''
        tag, content = value.get('t'), value.get('c')
        if tag == 'Str':
            return content
        if tag in {'Space', 'SoftBreak', 'LineBreak'}:
            return ' '
        if tag in {'Code', 'Math'}:
            return content[1]
        return inline_text(content)
    first = next((n for n in document.node_index if n.node_type == 'Header'), None)
    if first is None:
        return candidate_filename(None)
    return candidate_filename(inline_text(at_path(document.ast.to_dict(), first.path)['c'][2]))


def safe_target(path: Path):
    original = Path(path)
    if ((original.drive and not original.is_absolute())
            or any(part != part.rstrip('. ') for part in original.parts if part != original.anchor)):
        raise failure("OUTPUT_PATH_INVALID", "输出路径含不支持的名称。", "选择普通本地文件名。", "commit")
    path = Path(os.path.abspath(path))
    if path.suffix.lower() != ".docx":
        raise failure("OUTPUT_PATH_INVALID", "输出文件必须使用 .docx 扩展名。", "重新选择输出文件。", "commit")
    # Reject ADS, device names, trailing-dot aliases and Windows invalid characters.
    for part in path.parts[1:]:
        if (not part or part != part.rstrip(". ") or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part)):
            raise failure("OUTPUT_PATH_INVALID", "输出路径含不支持的名称。", "选择普通本地文件名。", "commit")
    if path.drive.startswith("\\\\"):
        raise failure("OUTPUT_PATH_UNSUPPORTED", "当前安全提交仅支持本地磁盘。", "选择本地目录。", "commit")
    reject_reparse(path)
    return path


def _identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class FileFingerprint:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str


def hash_stream(stream, check=lambda: None):
    digest = hashlib.sha256()
    size = 0
    while True:
        check()
        block = stream.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        if size > 256 * 1024 * 1024:
            raise failure("OUTPUT_TOO_LARGE", "待核对文件超过安全处理上限。", "另选输出路径。", "commit")
        digest.update(block)
    return digest.hexdigest()


def fingerprint(path: Path, check=lambda: None) -> FileFingerprint:
    reject_reparse(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        value = hash_stream(stream, check)
        after = os.fstat(stream.fileno())
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if not stat.S_ISREG(after.st_mode) or fields(before) != fields(after) or _identity(path) != fields(after)[:2]:
        raise failure("OUTPUT_CONFLICT", "输出目标在核对时发生变化。", "重新选择并确认目标。", "commit")
    return FileFingerprint(*fields(after), value)


@dataclass(frozen=True)
class OutputIntent:
    path: Path
    overwrite_authorized: bool
    expected: FileFingerprint | None
    parent_identity: tuple[int, int]

    @classmethod
    def capture(cls, path: Path, *, overwrite=False):
        path = safe_target(path)
        try:
            parent = _identity(path.parent)
        except OSError as exc:
            raise output_error(exc) from None
        expected = None
        if overwrite:
            try:
                expected = fingerprint(path)
            except FileNotFoundError:
                raise failure("OUTPUT_CONFLICT", "确认覆盖的文件已不存在。", "重新选择输出路径。", "commit") from None
            except OSError as exc:
                raise output_error(exc) from None
        elif path.exists():
            raise failure("OUTPUT_EXISTS", "输出文件已存在。", "选择其他路径或明确确认覆盖。", "commit")
        return cls(path, bool(overwrite), expected, parent)

    def __post_init__(self):
        if not self.path.is_absolute() or self.overwrite_authorized != (self.expected is not None):
            raise ValueError("Invalid output intent")


def output_error(exc):
    code = getattr(exc, "winerror", None)
    if isinstance(exc, FileExistsError) or code in (80, 183):
        return failure("OUTPUT_CONFLICT", "输出位置出现了同名文件。", "选择其他路径重试，转换结果暂时保留。", "commit")
    if code in (32, 33):
        return failure("OUTPUT_LOCKED", "输出文件被其他程序占用。", "关闭占用程序或选择其他路径。", "commit")
    if code in (39, 112) or getattr(exc, "errno", None) == 28:
        return failure("OUTPUT_DISK_FULL", "输出磁盘空间不足。", "释放空间或选择其他磁盘重试。", "commit")
    if isinstance(exc, PermissionError):
        return failure("OUTPUT_ACCESS_DENIED", "无法写入所选目录。", "选择有写入权限的目录重试。", "commit")
    return failure("OUTPUT_IO_FAILED", "输出提交未完成。", "检查磁盘和目录后重试，转换结果暂时保留。", "commit")


@contextmanager
def locked_read(path):
    """Reject Word/writer handles; never bypass an application's sharing protection."""
    if os.name != "nt":
        raise failure("OUTPUT_OVERWRITE_UNSAFE", "此平台未启用安全覆盖。", "另选不存在的文件名。", "commit")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


class OutputCommitter:
    """Stage in the destination directory, flush, then atomic fail-if-exists rename.

    No cross-process compare-and-swap exists here. Existing-target replacement is
    deliberately refused: a check followed by os.replace could clobber a newer file.
    """
    def commit(self, artifact: Path, expected_sha: str, intent: OutputIntent, context):
        stage = None
        try:
            path = safe_target(intent.path)
            if _identity(path.parent) != intent.parent_identity:
                raise failure("OUTPUT_CONFLICT", "输出目录已发生变化。", "重新选择目标目录。", "commit")
            context.check()
            if intent.overwrite_authorized:
                try:
                    with locked_read(path):
                        actual = fingerprint(path, context.check)
                        if actual != intent.expected:
                            raise failure("OUTPUT_CONFLICT", "确认后目标文件发生了变化。", "重新确认目标或另存。", "commit")
                        raise failure("OUTPUT_OVERWRITE_UNSAFE", "当前平台无法在保持目标保护时可靠替换已有文件。", "另选不存在的文件名；已验证结果暂时保留。", "commit")
                except FileNotFoundError:
                    raise failure("OUTPUT_CONFLICT", "确认后目标文件已不存在。", "重新选择目标或另存；已验证结果暂时保留。", "commit") from None
            fd, name = tempfile.mkstemp(prefix=".md2word-", suffix=".tmp", dir=path.parent)
            stage = Path(name)
            with os.fdopen(fd, "wb") as stream, artifact.open("rb") as source:
                digest = hashlib.sha256()
                while True:
                    context.check()
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    digest.update(block)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != expected_sha:
                raise failure("ARTIFACT_CHANGED", "暂存成品发生变化，停止提交。", "重新转换原文。", "commit")
            reject_reparse(path)
            if _identity(path.parent) != intent.parent_identity:
                raise failure("OUTPUT_CONFLICT", "输出目录已发生变化。", "重新选择目录。", "commit")
            context.advance("committing")  # Lock makes final cancellation check indivisible.
            # On Windows os.rename fails if destination exists; POSIX uses atomic
            # hardlink publication because POSIX rename would silently overwrite.
            if os.name == "nt":
                os.rename(stage, path)
            else:
                os.link(stage, path)
            return path
        except OSError as exc:
            raise output_error(exc) from None
        finally:
            if stage is not None:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    # Only this unique file is eligible; never remove a user path.
                    pass

