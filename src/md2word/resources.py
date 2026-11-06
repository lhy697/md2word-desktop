from pathlib import Path
import sys

class ResourceLocator:
    """Never fall back to PATH or write user data inside the installation."""
    def __init__(self, root: Path | None = None):
        self.root = (root or (
            Path(sys._MEIPASS) / "resources" if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2] / "resources"
        )).resolve()

    def file(self, relative: str) -> Path:
        result = (self.root / relative).resolve()
        if not result.is_relative_to(self.root):
            raise ValueError("Resource escapes application root")
        if not result.is_file():
            raise FileNotFoundError(f"Missing application resource: {relative}")
        return result

    @property
    def pandoc(self) -> Path:
        return self.file("pandoc/pandoc.exe")

    @staticmethod
    def data_root() -> Path:
        from PySide6.QtCore import QStandardPaths
        path = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.GenericDataLocation
        )) / "md2word"
        path.mkdir(parents=True, exist_ok=True)
        return path
