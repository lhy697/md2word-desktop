"""Shared product identity; never changes the existing user-data location."""
import os

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..resources import ResourceLocator


def configure_identity():
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("A QApplication is required")
    app.setApplicationVersion(__version__)
    icon = QIcon(str(ResourceLocator().file("ui/应用图标.png")))
    if icon.isNull():
        raise RuntimeError("Application icon is unreadable")
    app.setWindowIcon(icon)
    if os.name == "nt":
        import ctypes
        # Stable across updates so Windows groups this product consistently.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LocalTools.md2word")
