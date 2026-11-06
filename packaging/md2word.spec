from pathlib import Path
import PySide6

root = Path(SPECPATH).parent
used = {"QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtWebEngineCore", "QtWebEngineWidgets",
        "QtWebChannel", "QtPrintSupport", "QtTest", "QtOpenGL", "QtOpenGLWidgets"}
excluded = sorted("PySide6." + name for name in PySide6.__all__ if name not in used)
a = Analysis([str(root / "packaging/entry.py")], pathex=[str(root / "src")],
    binaries=[(str(root / "resources/pandoc/pandoc.exe"), "resources/pandoc")],
    datas=[(str(root / "resources"), "resources")],
    hiddenimports=["PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "docx", "pydantic"],
    excludes=["tkinter", "pytest"] + excluded, noarchive=False)
a.binaries = [item for item in a.binaries if "virtualkeyboard" not in item[0].lower()]
a.datas = [item for item in a.datas if "virtualkeyboard" not in item[0].lower()]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="md2word", console=False,
    debug=False, icon=str(root / "packaging/app.ico"), version=str(root / "packaging/windows-version.txt"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="md2word")
