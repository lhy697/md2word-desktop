import json
from pathlib import Path
from PySide6.QtCore import QUrl, Signal, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QComboBox
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from ..live_preview import external_link
from ..resources import ResourceLocator


class ResourcePolicy(QWebEngineUrlRequestInterceptor):
    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = root.resolve()
        names = ["workspace.html", "workspace.css", "workspace.js", "gsap/gsap.min.js"]
        self.files = {self.root / name for name in names}
        self.files.update(p.resolve() for p in (self.root / "mathjax").rglob("*")
                          if p.is_file() and p.suffix in {".js", ".woff", ".woff2", ".json"})
        self.allowed = set()
        self.blocked = set()

    def permits(self, url):
        if not url.isLocalFile() or url.host() or url.hasQuery():
            return False
        try:
            path = Path(url.toLocalFile()).resolve()
            return path in self.files and path.is_relative_to(self.root) and path.is_file()
        except (ValueError, OSError):
            return False

    def interceptRequest(self, info):
        url = info.requestUrl()
        if self.permits(url):
            self.allowed.add(Path(url.toLocalFile()).relative_to(self.root).as_posix())
        else:
            self.blocked.add(url.scheme())
            info.block(True)


class RestrictedPage(QWebEnginePage):
    def __init__(self, profile, index, parent=None):
        super().__init__(profile, parent)
        self.index = index.resolve()
        self.errors = []

    def acceptNavigationRequest(self, url, kind, main):
        if (main and url.isLocalFile() and not url.hasQuery() and not url.host()
                and Path(url.toLocalFile()).resolve() == self.index):
            return True
        if main and kind == self.NavigationType.NavigationTypeLinkClicked and external_link(url.toString()):
            QDesktopServices.openUrl(url)
        return False

    def createWindow(self, kind):
        return None

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == self.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append("PREVIEW_SCRIPT_ERROR")


class PreviewPanel(QWidget):
    renderProblem = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        root = ResourceLocator().file("preview/workspace.html").parent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("panelBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 12, 16, 12)
        row.addWidget(QLabel("样式预览"))
        row.addStretch()
        self.zoom = QComboBox()
        for label, value in [("适应宽度", 1.0), ("放大 125%", 1.25), ("放大 150%", 1.5)]:
            self.zoom.addItem(label, value)
        self.zoom.setMaximumWidth(126)
        row.addWidget(self.zoom)
        layout.addWidget(bar)
        self.notice = QLabel("样式预览，分页以 Word 为准")
        self.notice.setObjectName("previewNotice")
        self.notice.setMargin(10)
        layout.addWidget(self.notice)
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.policy = ResourcePolicy(root, self.profile)
        self.profile.setUrlRequestInterceptor(self.policy)
        self.profile.downloadRequested.connect(lambda download: download.cancel())
        self.page = RestrictedPage(self.profile, root / "workspace.html", self)
        settings = self.page.settings()
        attr = QWebEngineSettings.WebAttribute
        for name in ("LocalContentCanAccessRemoteUrls", "JavascriptCanOpenWindows", "JavascriptCanAccessClipboard", "LocalStorageEnabled", "FullScreenSupportEnabled", "ScreenCaptureEnabled", "HyperlinkAuditingEnabled", "WebGLEnabled", "PluginsEnabled", "PdfViewerEnabled"):
            settings.setAttribute(getattr(attr, name), False)
        settings.setAttribute(attr.LocalContentCanAccessFileUrls, True)
        self.page.featurePermissionRequested.connect(lambda origin, feature: self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser))
        self.view = QWebEngineView()
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.view.setPage(self.page)
        layout.addWidget(self.view, 1)
        self.loaded = False
        self.queued = None
        self.motion = True
        self.selection_restore = None
        self.page.loadFinished.connect(self._loaded)
        self.zoom.currentIndexChanged.connect(self._zoom)
        self.view.load(QUrl.fromLocalFile(str(root / "workspace.html")))

    def _loaded(self, ok):
        self.loaded = ok
        if not ok:
            self.renderProblem.emit("预览资源加载失败，请检查本地资源。")
        elif self.queued:
            self.render(self.queued)

    def _zoom(self):
        if self.loaded:
            self.page.runJavaScript(f"window.setPreviewZoom({self.zoom.currentData()});")

    def invalidate(self):
        self.queued = None
        if self.loaded:
            self.page.runJavaScript("window.invalidatePreview();")

    def render(self, result):
        self.queued = result
        if not self.loaded:
            return
        data = json.dumps(dict(html=result.html, css=result.css,
                               revision=self.revision_token(result.key),
                               selectionRestore=self.selection_restore[2] if self.selection_restore and self.selection_restore[:2] == (result.key.session_id,result.key.content_revision) else None), ensure_ascii=True)
        self.selection_restore = None
        self.page.runJavaScript(f"window.setPreviewMotion({str(self.motion).lower()});window.renderDocument({data});")

    @staticmethod
    def revision_token(key):
        token = f"{key.session_id}:{key.content_revision}:{key.style_revision}:{','.join(key.fallbacks)}"
        return token + (":"+key.local_hash if key.local_hash else "")

    def set_motion(self, enabled):
        self.motion = enabled
        if self.loaded:
            self.page.runJavaScript(f"window.setPreviewMotion({str(enabled).lower()});")

    def dispose(self):
        # Destroy WebEngine objects synchronously on the GUI thread. Deferred
        # deletion alone can leave Python cycles to a worker's garbage collector.
        from shiboken6 import delete
        self.view.stop()
        delete(self.view)
        delete(self.page)
        delete(self.profile)
