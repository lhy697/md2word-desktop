"""OS font inventory and explicit choices; never installs or copies fonts."""
import ctypes
from dataclasses import dataclass
import os
from ctypes import wintypes
from functools import cache
from ..domain.models import Issue, Severity


if os.name == "nt":
    class _LOGFONT(ctypes.Structure):
        _fields_ = [(name, wintypes.LONG) for name in (
            "height", "width", "escapement", "orientation", "weight")] + [
            (name, wintypes.BYTE) for name in (
                "italic", "underline", "strikeout", "charset",
                "outprecision", "clipprecision", "quality", "pitch")] + [
            ("face", wintypes.WCHAR * 32)]

    _FONTENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.POINTER(_LOGFONT),
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.c_ssize_t)


@cache
def _font_api():
    # Private DLL/function objects: no mutation of ctypes.windll's shared
    # signatures. Concurrent first calls are harmless: all use the fixed types.
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    get_dc, release_dc, enumerate_fonts = user.GetDC, user.ReleaseDC, gdi.EnumFontFamiliesExW
    get_dc.restype = ctypes.c_void_p
    get_dc.argtypes = [ctypes.c_void_p]
    release_dc.restype = ctypes.c_int
    release_dc.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    enumerate_fonts.restype = ctypes.c_int
    enumerate_fonts.argtypes = [ctypes.c_void_p, ctypes.POINTER(_LOGFONT),
                               _FONTENUMPROC, ctypes.c_ssize_t, wintypes.DWORD]
    return get_dc, release_dc, enumerate_fonts

@dataclass(frozen=True)
class MissingFont:
    name: str
    fields: tuple[str, ...]
    alternatives: tuple[str, ...]

@dataclass(frozen=True)
class FontReport:
    missing: tuple[MissingFont, ...]
    issues: tuple[Issue, ...]

def system_fonts() -> frozenset[str] | None:
    if os.name != "nt":
        return None
    values = set()
    @_FONTENUMPROC
    def receive(font, metrics, kind, param):
        values.add(font.contents.face.lstrip("@"))
        return 1
    try:
        get_dc, release_dc, enumerate_fonts = _font_api()
        dc = get_dc(None)
        if not dc:
            return None
        try:
            query = _LOGFONT()
            query.charset = 1
            enumerate_fonts(dc, ctypes.byref(query), receive, 0, 0)
        finally:
            release_dc(None, dc)
        return frozenset(values) if values else None
    except (OSError, ctypes.ArgumentError):
        # Inventory is advisory; unavailable OS enumeration must not fail export.
        return None

def check_fonts(style, available=None) -> FontReport:
    inventory = system_fonts() if available is None else frozenset(available)
    if inventory is None:
        return FontReport((), (Issue("FONT_CHECK_UNAVAILABLE", Severity.INFO, "style",
            "当前无法枚举系统字体。", "在 Word 中检查字体替代。"),))
    by_case = {name.casefold(): name for name in inventory}
    required = {}
    raw = style.config.model_dump(mode="json")
    def visit(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"font", "font_zh", "font_latin"}:
                    required.setdefault(item, []).append(path + key)
                else:
                    visit(item, path + key + ".")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, path + str(i + 1) + ".")
    visit(raw)
    missing, issues = [], []
    for font, fields in required.items():
        if font.casefold() in by_case:
            continue
        preferred = ("宋体", "SimSun", "Microsoft YaHei", "Arial", "Times New Roman", "Consolas", "Cambria Math")
        alternatives = tuple(by_case[x.casefold()] for x in preferred if x.casefold() in by_case)[:4]
        missing.append(MissingFont(font, tuple(fields), alternatives))
        issues.append(Issue("FONT_MISSING", Severity.WARNING, "style",
            "所选字体未安装：" + font + "。",
            "可选择：" + "、".join(alternatives) if alternatives else "请选择本机已有字体。"))
    return FontReport(tuple(missing), tuple(issues))


def check_local_fonts(values):
    required = {font for paragraph in values for run in paragraph.runs
                for font in (run.format.font_zh,run.format.font_latin) if font}
    if not required:
        return ()
    inventory = system_fonts()
    if inventory is None:
        return (Issue("FONT_CHECK_UNAVAILABLE",Severity.INFO,"style","当前无法枚举系统字体。","在 Word 中检查字体替代。"),)
    installed = {name.casefold() for name in inventory}
    return tuple(Issue("FONT_MISSING",Severity.WARNING,"style","局部排版所选字体未安装："+name+"。",
                       "选择本机字体后重新应用，或在 Word 中检查字体替代。") for name in sorted(required) if name.casefold() not in installed)
