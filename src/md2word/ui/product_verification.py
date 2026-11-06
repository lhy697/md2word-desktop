"""Opt-in final-product checks. Uses only fixed data and an isolated profile."""
import ctypes
from ctypes import wintypes
import hashlib
from html import escape
import json
from pathlib import Path
import platform
import time

from PySide6.QtCore import QTimer, QUrl, qVersion
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from .. import __version__
from ..domain.models import DocumentSession
from ..exporting.models import TaskState
from ..exporting.output import OutputIntent
from ..exporting.scheduler import Scheduler
from ..exporting.service import ExportService
from ..resources import ResourceLocator
from ..storage.files import UserPaths
from ..templates.service import PersonalStyleRepository
from .main_window import MainWindow
from .verification import javascript, wait_for


def peak_working_set():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    api = ctypes.WinDLL("psapi", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.PeakWorkingSetSize


def verify_product(output_directory):
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=False)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("md2word-product-qa")
    paths = UserPaths(output / "isolated-user-data")
    window = MainWindow(user_paths=paths, style_repository=PersonalStyleRepository(paths.styles),
                        scheduler=Scheduler(ExportService(work_root=paths.tasks)))
    window.may_replace = lambda: True
    window.motion.setChecked(False)
    window.show()
    report = {"passed": False, "version": __version__, "qt": qVersion(),
              "windows": platform.platform(), "python": platform.python_version(),
              "elevated": bool(ctypes.windll.shell32.IsUserAnAdmin()),
              "user_data_isolated": True, "clean_machine": False,
              "physical_offline": False, "timings": {}, "benchmark": {}}
    credits_page = None
    credits_profile = None
    try:
        wait_for(lambda: window.preview.loaded)
        report["window_ready_perf_counter"] = time.perf_counter()
        icon = ResourceLocator().file("ui/应用图标.png")
        report["icon_sha256"] = hashlib.sha256(icon.read_bytes()).hexdigest()
        report["window_icon_set"] = not window.windowIcon().isNull()
        report["application_version"] = app.applicationVersion()
        report["brand_uses_icon"] = not window.brand_mark.pixmap().isNull()
        original_open = QDesktopServices.openUrl
        urls = []
        QDesktopServices.openUrl = lambda url: urls.append(url.toLocalFile()) or True
        try:
            for label in ("打开使用手册", "第三方声明"):
                def choose(label=label):
                    modal = QApplication.activeModalWidget()
                    if isinstance(modal, QMessageBox):
                        next(button for button in modal.buttons() if button.text() == label).click()
                QTimer.singleShot(100, choose)
                window.show_help()
        finally:
            QDesktopServices.openUrl = original_open
        report["help_links_valid"] = len(urls) == 2 and all(Path(url).is_file() for url in urls)
        demo = ResourceLocator().file("examples/product-demo.md").read_text("utf-8")
        window.replace_session(DocumentSession.create(demo), "演示文档")
        wait_for(lambda: window.latest_preview is not None and window.latest_preview.key == window.current_key(), 40)
        wait_for(lambda: javascript(window, "window.previewState.ready && window.previewState.mathCount >= 1"), 25)
        window.resize(1480, 960)
        app.processEvents()
        window.grab().save(str(output / "主界面.png"))
        window.style_tabs.setCurrentIndex(1)
        app.processEvents()
        window.grab().save(str(output / "局部排版入口.png"))
        window.style_tabs.setCurrentIndex(0)
        window.start_export(OutputIntent.capture(output / "演示输出.docx"))
        wait_for(lambda: window.handle is None and window.last_result is not None, 75)
        report["demo_export_passed"] = window.last_result.state == TaskState.SUCCEEDED
        report["demo_export_sha256"] = hashlib.sha256((output / "演示输出.docx").read_bytes()).hexdigest()
        # About 10k Chinese characters and exactly 30 formulas, no private input.
        prose = "本地文档排版需要保留正文、数字和结构。测试检查中文与英文混排，确认原始资料不会被自动覆盖。"
        source = "# 固定性能样例\n\n" + "\n\n".join([prose * 4 for _ in range(55)])
        source += "\n\n| 项目 | 数值 |\n|---|---|\n| 基准 | 2026 |\n| 保留 | 3.14159 |\n\n"
        source += "\n\n".join("$$x_{" + str(i) + r"}^2+\frac{1}{" + str(i+1) + "}$$" for i in range(30))
        (output / "固定性能样例.md").write_text(source, "utf-8")
        report["benchmark"] = {"characters": len(source), "cjk_characters": sum('\u4e00' <= ch <= '\u9fff' for ch in source),
                               "formulas": 30, "table_data_rows": 2, "cache": "new document; same process had demo preview/export"}
        start = time.perf_counter()
        window.replace_session(DocumentSession.create(source), "固定性能样例")
        wait_for(lambda: window.latest_preview is not None and window.latest_preview.key == window.current_key(), 75)
        wait_for(lambda: javascript(window, "window.previewState.ready && window.previewState.mathCount === 30"), 40)
        report["timings"]["first_benchmark_render_seconds"] = time.perf_counter() - start
        start = time.perf_counter()
        window.request_preview()
        wait_for(lambda: window.preview_lane.idle and window.latest_preview.key == window.current_key(), 45)
        report["timings"]["cached_preview_request_seconds"] = time.perf_counter() - start
        window.grab().save(str(output / "公式与表格.png"))
        for ordinal in (1, 2):
            previous = window.last_result
            start = time.perf_counter()
            window.start_export(OutputIntent.capture(output / f"benchmark-{ordinal}.docx"))
            wait_for(lambda: window.handle is None and window.last_result is not previous, 90)
            report["timings"][f"benchmark_export_{ordinal}_seconds"] = time.perf_counter() - start
            if window.last_result.state != TaskState.SUCCEEDED:
                raise RuntimeError("BENCHMARK_EXPORT_FAILED")
        report["main_process_peak_working_set_bytes"] = peak_working_set()
        report["memory_scope"] = "main process peak working set only; excludes Chromium and Pandoc children"
        # Qt WebEngine exposes version information at chrome://qt. Unlike Chrome,
        # this build does not implement chrome://credits; do not save its error page
        # as purported license notices.
        credits_profile = QWebEngineProfile(app)
        credits_page = QWebEnginePage(credits_profile, app)
        loaded = []
        credits_page.loadFinished.connect(loaded.append)
        credits_page.setUrl(QUrl("chrome://qt"))
        wait_for(lambda: bool(loaded), 40)
        texts = []
        credits_page.runJavaScript("document.getElementById('log')?.remove(); document.body.innerText", texts.append)
        wait_for(lambda: bool(texts), 15)
        text = texts[0] or ""
        report["embedded_webengine_version"] = text
        if "Chromium" not in text or "6.8.3" not in text:
            raise RuntimeError("WEBENGINE_VERSION_MISSING")
        (output / "QtWebEngine-version.txt").write_text(text, "utf-8")
        # Verify that the quoted Chromium host rule really takes effect.
        statuses = []
        credits_page.loadingChanged.connect(lambda info: statuses.append(info.errorCode()))
        loaded.clear()
        credits_page.setUrl(QUrl("https://example.com/"))
        wait_for(lambda: bool(loaded), 20)
        report["chromium_dns_probe_error_codes"] = statuses
        report["chromium_dns_probe_blocked"] = -105 in statuses and loaded[-1] is False
        if not report["chromium_dns_probe_blocked"]:
            raise RuntimeError("DNS_ISOLATION_NOT_EFFECTIVE")
        window.resize(800, 640)
        app.processEvents()
        window.grab().save(str(output / "小窗口.png"))
        report["passed"] = all(report[k] for k in ("window_icon_set", "brand_uses_icon", "help_links_valid", "demo_export_passed")) and report["application_version"] == __version__
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if credits_page is not None:
            credits_page.deleteLater()
        if credits_profile is not None:
            credits_profile.deleteLater()
        window.persistence.close_failure_choice = lambda: "discard"
        window.close()
        wait_for(lambda: window.disposed, 65)
        app.processEvents()
        (output / "product-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return 0 if report["passed"] else 1
