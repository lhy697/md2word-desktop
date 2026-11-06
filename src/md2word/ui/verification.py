"""Opt-in fixed nonprivate UI verification, also runnable inside the frozen app."""
import ctypes
import json
from pathlib import Path
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from ..domain.models import DocumentSession, InputMode
from ..exporting.models import TaskState
from ..exporting.output import OutputIntent
from ..exporting.scheduler import Scheduler
from ..exporting.service import ExportService
from ..templates.service import PersonalStyleRepository
from .main_window import MainWindow


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("UI_VERIFICATION_TIMEOUT")
        loop = QEventLoop()
        QTimer.singleShot(30, loop.quit)
        loop.exec()


def javascript(window, expression):
    result = []
    window.preview.page.runJavaScript(expression, result.append)
    wait_for(lambda:bool(result), 10)
    return result[0]


def verify_gui(output_directory):
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=False)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("md2word-qa")
    app.setOrganizationName("LocalTools")
    report = {"passed":False, "physical_network_disconnected":False,
              "clean_machine":False, "elevated":bool(ctypes.windll.shell32.IsUserAnAdmin()),
              "sample":"bundled lecture-motion.md; synthetic click/edit race",
              "source_upload":False}
    with tempfile.TemporaryDirectory(prefix="md2word-ui-qa-") as temp:
        root = Path(temp)
        window = MainWindow(style_repository=PersonalStyleRepository(root / "styles"),
                            scheduler=Scheduler(ExportService(work_root=root / "jobs")))
        window.may_replace = lambda:True
        window.show()
        try:
            wait_for(lambda:window.preview.loaded)
            window.motion.setChecked(False)
            window.example_button.click()
            window.request_preview()
            wait_for(lambda:window.latest_preview is not None)
            parses = window.preview_lane.service.parse_count
            size_picker = window.styles.fields["body.size_pt"]
            size_picker.setCurrentIndex(size_picker.findData(14.0))
            report["typography_picker"] = {
                "size_label":size_picker.currentText(),
                "size_pt":size_picker.points(),
                "font_items":window.styles.fields["body.font_zh"].count(),
                "sample":window.styles.font_preview.toPlainText(),
            }
            assert size_picker.points() == 14.0 and "四号" in size_picker.currentText()
            assert "Word Preview" in window.styles.font_preview.toPlainText()
            window.request_preview()
            wait_for(lambda:window.latest_preview.key == window.current_key())
            report["style_only_reuses_ast"] = window.preview_lane.service.parse_count == parses
            wait_for(lambda:bool(javascript(window, "window.previewState.ready")))
            window.motion.setChecked(False)
            settle = QEventLoop()
            QTimer.singleShot(350, settle.quit)
            settle.exec()
            report["preview"] = json.loads(javascript(window, "JSON.stringify(window.previewState)"))
            report["layout"] = json.loads(javascript(window, "JSON.stringify({width:innerWidth,clientWidth:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,paperRight:document.getElementById('paper').getBoundingClientRect().right,opacity:getComputedStyle(document.getElementById('paper')).opacity})"))
            report["console_errors"] = list(window.preview.page.errors)
            report["allowed_resources"] = sorted(window.preview.policy.allowed)
            report["blocked_schemes"] = sorted(window.preview.policy.blocked)
            example_target = output / "内置示例 课程讲义.docx"
            window.start_export(OutputIntent.capture(example_target))
            wait_for(lambda:window.last_result is not None)
            example_result = window.last_result
            report["example_export_state"] = example_result.state.value
            report["example_style_size_pt"] = window.styles.style.config.body.size_pt
            with zipfile.ZipFile(example_target) as archive:
                doc = ET.fromstring(archive.read("word/document.xml"))
                report["example_math_objects"] = len(doc.findall('.//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath'))
            report["desktop_size"] = [window.width(), window.height()]
            window.grab().save(str(output / "desktop.png"))
            window.resize(720, 680)
            wait_for(lambda:window.compact)
            window.tabs.setCurrentWidget(window.preview)
            wait_for(lambda:window.preview.view.width() > 500)
            # Let the actual WebEngine resize and Qt layout settle.
            settle = QEventLoop()
            QTimer.singleShot(350, settle.quit)
            settle.exec()
            window.grab().save(str(output / "small-preview.png"))
            report["small_size"] = [window.width(), window.height()]
            report["small_tabs"] = window.tabs.count()
            report["small_horizontal_overflow"] = javascript(window, "document.documentElement.scrollWidth-innerWidth")
            window.tabs.setCurrentWidget(window.editor_panel)
            window.grab().save(str(output / "small-editor.png"))
            window.resize(1440, 960)
            window.replace_session(DocumentSession.create("# 导出快照验证\n\n点击时数字 12345\n"), "固定快照验证")
            target = output / "中文 课程讲义.docx"
            window.start_export(OutputIntent.capture(target))
            window.editor.setPlainText("# 当前编辑\n\n继续编辑的数字 99999\n")
            wait_for(lambda:window.last_result is not example_result)
            report["export_state"] = window.last_result.state.value
            report["export_notice"] = window.status.text()
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode()
            report["click_snapshot_preserved"] = "12345" in xml and "99999" not in xml and window.session.dirty
            report["passed"] = (report["preview"]["ready"] and report["preview"]["mathCount"] > 0
                                and report["preview"]["gsap"] == "3.13.0" and not report["preview"]["errors"]
                                and not report["console_errors"] and report["small_tabs"] == 2
                                and report["small_horizontal_overflow"] <= 2
                                and report["export_state"] == TaskState.SUCCEEDED.value
                                and report["click_snapshot_preserved"])
            report["passed"] &= (report["style_only_reuses_ast"] and report["example_math_objects"] == 14
                                 and report["example_export_state"] == TaskState.SUCCEEDED.value)
            report["review4_cases"] = []
            cases = [
                ("plain-literal", InputMode.PLAIN, r"纯文本应保留：\(x^2\) 与 \[y=1\]", 0,
                 [r"\(x^2\)", r"\[y=1\]"]),
                ("markdown-literal", InputMode.MARKDOWN, r"普通字面文字：\\(x^2\\)", 0, [r"\(x^2\)"]),
                ("mixed-math", InputMode.MARKDOWN, r"字面 \\(literal\\)；实际 $x^2$", 1, [r"\(literal\)"]),
                ("nested-image", InputMode.MARKDOWN,
                 '[**![badge](https://example.org/badge.png "image title")**](./report.pdf)', 0,
                 ["badge", "https://example.org/badge.png", "image title", "./report.pdf"]),
            ]
            for name, mode, source, expected_math, preserved in cases:
                report["verification_phase"] = name + ": preview"
                window.replace_session(DocumentSession.create(source, mode), name)
                window.request_preview()
                wait_for(lambda:window.latest_preview is not None and window.latest_preview.key == window.current_key())
                wait_for(lambda:bool(javascript(window, "window.previewState.ready")))
                state = json.loads(javascript(window, "JSON.stringify(window.previewState)"))
                preview_text = javascript(window, "document.getElementById('paper').textContent")
                previous = window.last_result
                report["verification_phase"] = name + ": export"
                path = output / (name + ".docx")
                window.start_export(OutputIntent.capture(path))
                wait_for(lambda:window.last_result is not previous)
                with zipfile.ZipFile(path) as archive:
                    doc = ET.fromstring(archive.read("word/document.xml"))
                docx_text = ''.join(n.text or '' for n in doc.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
                omml = len(doc.findall('.//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath'))
                passed = (state["mathCount"] == expected_math and not state["errors"] and omml == expected_math
                          and all(value in preview_text and value in docx_text for value in preserved)
                          and window.last_result.state == TaskState.SUCCEEDED)
                report["review4_cases"].append(dict(case=name, passed=passed, math_count=state["mathCount"], omml=omml,
                                                    literals_preserved=all(value in preview_text and value in docx_text for value in preserved)))
                report["passed"] &= passed
            report["verification_phase"] = "complete"
        except Exception as exc:
            report["passed"] = False
            report["error"] = getattr(exc, "code", type(exc).__name__)
        finally:
            window.close()
            wait_for(lambda:window.disposed)
            app.processEvents()
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return 0 if report["passed"] else 1
