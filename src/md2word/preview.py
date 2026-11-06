"""Fixed local HTML spike. No editor or user-provided HTML in stage one."""
import ctypes
import json
from pathlib import Path
import threading
import time
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                    QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView
from .resources import ResourceLocator

class LocalOnlyInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = root.resolve()
        self.allowed = set()
        self.blocked = []
        self.lock = threading.Lock()

    def interceptRequest(self, info):
        url = info.requestUrl()
        path = Path(url.toLocalFile()).resolve() if url.isLocalFile() else None
        valid = path is not None and path.is_relative_to(self.root) and path.is_file()
        with self.lock:
            if valid:
                self.allowed.add(path.relative_to(self.root).as_posix())
            else:
                self.blocked.append(url.scheme())  # no arbitrary paths or source
        if not valid:
            info.block(True)

class PreviewPage(QWebEnginePage):
    def __init__(self, profile, index, parent=None):
        super().__init__(profile, parent)
        self.index = index
        self.errors = []

    def acceptNavigationRequest(self, url, kind, main):
        return url.isLocalFile() and Path(url.toLocalFile()).resolve() == self.index

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append(message[:400])

def show_preview(report_path: Path | None = None, screenshot: Path | None = None):
    started = time.monotonic()
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("LocalTools")
    app.setApplicationName("md2word-stage1")
    resources = ResourceLocator()
    index = resources.file("preview/index.html")
    profile = QWebEngineProfile(app)  # off-the-record: no persistent browser data
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
    interceptor = LocalOnlyInterceptor(index.parent, profile)
    profile.setUrlRequestInterceptor(interceptor)
    profile.downloadRequested.connect(lambda download: download.cancel())
    page = PreviewPage(profile, index)
    view = QWebEngineView()
    view.setPage(page)
    window = QMainWindow()
    window.setWindowTitle("Markdown 转 Word · 阶段一技术验证")
    window.resize(1050, 760)
    window.setCentralWidget(view)
    window.show()
    view.load(QUrl.fromLocalFile(str(index)))
    timer = QTimer(window)
    done = False

    def finish(state, passed):
        nonlocal done
        if done:
            return
        done = True
        timer.stop()
        with interceptor.lock:
            allowed, blocked = sorted(interceptor.allowed), list(interceptor.blocked)
        report = {
            "passed": passed, "render": state,
            "elapsed_seconds": time.monotonic() - started,
            "allowed_resources": allowed, "blocked_schemes": blocked,
            "console_errors": page.errors,
            "network_policy": "only files within bundled preview root allowed",
            "physical_network_disconnected": False,
            "elevated": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        }
        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            view.grab().save(str(screenshot.resolve()))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        app.exit(0 if passed else 1)

    def observed(state):
        if done:
            return
        if isinstance(state, str) and state:
            state = json.loads(state)
        if isinstance(state, dict) and state.get("ready"):
            passed = (state.get("math_count") == 2 and state.get("errors") == 0
                      and state.get("fonts_loaded", 0) > 0 and not page.errors)
            finish(state, passed)
        elif time.monotonic() - started > 30:
            finish(state, False)

    if report_path:
        def poll():
            page.runJavaScript("""JSON.stringify({
                ready: window.previewReady === true,
                math_count: document.querySelectorAll('mjx-container').length,
                errors: document.querySelectorAll('mjx-merror').length,
                fonts_loaded: Array.from(document.fonts).filter(f=>f.status==='loaded').length,
                font_status: document.fonts.status,
                mathjax: window.MathJax && MathJax.version
            })""", observed)
        timer.timeout.connect(poll)
        timer.start(500)
        QTimer.singleShot(35000, lambda: finish({"error": "verification timed out"}, False))
    result = app.exec()
    # Explicit ownership teardown keeps profile alive until page destruction.
    view.setPage(None)
    page.deleteLater()
    window.close()
    app.processEvents()
    return result
