"""Stage 06: real HTML/Qt/Word pipeline plus explicitly controlled race windows."""
from dataclasses import replace
from pathlib import Path
import json
import threading
import time
import zipfile

import pytest
from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog
from md2word.domain.models import DocumentSession, InputMode, DocumentError
from md2word.domain.repairs import suggest_outer_fence
from md2word.domain.source_mapping import qt_selection
from md2word.templates.service import resolve_style, PersonalStyleRepository
from md2word.exporting.models import TaskState
from md2word.exporting.output import OutputIntent
from md2word.exporting.scheduler import Scheduler
from md2word.exporting.service import ExportService
from md2word.live_preview import PreviewService, PreviewKey, PreviewRequest, PreviewResult, PreviewLane, SafeHtml, style_css, external_link
from md2word.ui.main_window import MainWindow
from md2word.ui.preview_panel import ResourcePolicy
from md2word.resources import ResourceLocator

ROOT = Path(__file__).resolve().parents[1]


def request(service, session, style=None):
    style = style or resolve_style()
    key = PreviewKey(session.session_id, session.content_revision, session.style_revision,
                     service.parser.profile.fingerprint, style.resolved_hash, style.template_version)
    return PreviewRequest(key, session.snapshot(), style)


def spin(predicate, timeout=20000):
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    raise AssertionError("Timed out waiting for the expected UI state")


def js_value(window, expression):
    values = []
    window.preview.page.runJavaScript(expression, values.append)
    spin(lambda: bool(values), timeout=10000)
    return values[0]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    scheduler = Scheduler(ExportService(work_root=tmp_path / "jobs"))
    w = MainWindow(style_repository=PersonalStyleRepository(tmp_path / "styles"), scheduler=scheduler)
    monkeypatch.setattr(w, "may_replace", lambda: True)
    w.show()
    spin(lambda: w.preview.loaded)
    yield w
    w.close()
    spin(lambda: w.disposed)
    app.processEvents()


def test_real_preview_reuses_normalized_ast_for_style_only_changes():
    service = PreviewService()
    source = (ROOT / "tests/fixtures/stage3/F06_math.md").read_text("utf-8")
    session = DocumentSession.create(source)
    first = service.render(request(service, session), threading.Event())
    assert first.failure is None and first.math_unchecked == 9
    assert '<span class="math' in first.html and "<script" not in first.html
    style = resolve_style(overrides={"body":{"size_pt":14.0}})
    second = service.render(request(service, session.style_changed(), style), threading.Event())
    assert service.parse_count == 1
    assert second.document is first.document and "font-size:14.0pt" in second.css
    assert second.key != first.key


def test_raw_html_images_and_plain_source_are_inert():
    service = PreviewService()
    session = DocumentSession.create((ROOT / "tests/fixtures/stage2/F10_fallbacks.md").read_text("utf-8"))
    result = service.render(request(service, session), threading.Event())
    assert result.failure is None
    assert '<script' not in result.html and '<img' not in result.html
    assert '&lt;script' in result.html
    source = '<script>window.pwned=1</script>\n# 保留标题字面\n' + chr(36) + 'x' + chr(36)
    plain = DocumentSession.create(source, InputMode.PLAIN)
    result = service.render(request(service, plain), threading.Event())
    assert '&lt;script&gt;' in result.html and '<h1' not in result.html and result.math_unchecked == 0


@pytest.mark.parametrize("target", ["javascript:alert(1)", "file:///C:/secret.txt", "data:text/html,hi", "https://user:pass@example.com/", "https://example.com/\n"])
def test_link_policy_rejects_unsafe_targets(target):
    assert not external_link(target)


def test_sanitizer_drops_event_attributes_and_active_nodes():
    html = SafeHtml.clean('<p onclick="alert(1)">正文</p><img src="file:///C:/secret"><iframe src="https://example.com"></iframe><a href="javascript:alert(1)">链接</a>')
    assert html == '<p>正文</p><a>链接</a>'
    assert external_link("https://example.com/path?q=test")


def test_font_names_cannot_inject_css():
    style = resolve_style(overrides={"body":{"font_zh": 'Font";injected'}})
    css = style_css(style)
    assert 'Font";injected' not in css and r'\22 ' in css


def test_lane_is_latest_only_with_deliberately_late_old_result():
    gate = threading.Event()
    entered = threading.Event()
    calls = []
    class SlowService:
        def render(self, req, cancel):
            calls.append(req)
            entered.set()
            if len(calls) == 1:
                assert gate.wait(5)
            return req
    lane = PreviewLane(SlowService())
    try:
        lane.request("old")
        assert entered.wait(2)
        for n in range(50):
            lane.request(f"new-{n}")
        assert lane.pending == "new-49" and calls == ["old"]
        gate.set()
        deadline = time.monotonic() + 5
        seen = []
        while time.monotonic() < deadline and len(seen) < 2:
            result = lane.poll()
            if result:
                seen.append(result)
            time.sleep(.01)
        assert seen == ["old", "new-49"] and calls == seen
    finally:
        gate.set()
        lane.shutdown()


def test_resource_whitelist_restricts_exact_bundled_files(app, tmp_path):
    root = ResourceLocator().file("preview/workspace.html").parent
    policy = ResourcePolicy(root)
    assert policy.permits(QUrl.fromLocalFile(str(root / "mathjax/tex-chtml.js")))
    assert policy.permits(QUrl.fromLocalFile(str(root / "gsap/gsap.min.js")))
    assert not policy.permits(QUrl.fromLocalFile(str(root / "gsap/provenance.json")))
    assert not policy.permits(QUrl.fromLocalFile(str(ROOT / "README.md")))
    assert not policy.permits(QUrl("https://example.com/image.png"))
    assert not policy.permits(QUrl("data:text/plain,test"))


def test_qt_repair_one_undo_and_stale_suggestion(window):
    source = '```markdown\n# 课程资料\n\n保留原文\n```\n'
    window.replace_session(DocumentSession.create(source))
    suggestion = suggest_outer_fence(window.session.snapshot()).suggestion
    original = window.session.original_text
    window.apply_repair_suggestion(suggestion)
    assert window.editor.toPlainText() == suggestion.after
    window.editor.undo()
    assert window.editor.toPlainText() == source and window.session.original_text == original
    with pytest.raises(DocumentError, match="失效"):
        window.apply_repair_suggestion(suggestion)
    assert window.editor.toPlainText() == source


def test_style_invalid_input_blocks_export_and_personal_style_operations(window, monkeypatch):
    window.editor.setPlainText("# 标题\n\n正文")
    field = window.styles.fields["body.size_pt"]
    before = window.styles.style
    field.setEditText("NaN")
    assert not window.styles.valid and not window.export_button.isEnabled()
    assert window.styles.style == before
    field.setEditText("14")
    assert window.styles.valid and window.export_button.isEnabled()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw:("我的讲义", True))
    window.styles.save_style()
    identity = window.styles.templates.currentData()
    assert identity.startswith("user.") and window.styles.repository.load(identity).body.size_pt == 14
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw:("讲义新名称", True))
    window.styles.rename_style()
    assert window.styles.templates.currentText() == "讲义新名称"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw:QMessageBox.StandardButton.Yes)
    window.styles.delete_style()
    assert window.styles.templates.currentData() == "lecture"
    assert not window.styles.repository.path(identity).exists()


def test_typography_dropdowns_update_sample_document_preview_and_docx(window, tmp_path):
    from docx import Document
    from docx.oxml.ns import qn
    window.editor.setPlainText("正文示例 Font Preview 12345")
    fonts = window.styles.fields["body.font_zh"]
    family = "黑体" if fonts.findText("黑体") >= 0 else fonts.itemText(0)
    fonts.setCurrentIndex(fonts.findText(family))
    sizes = window.styles.fields["body.size_pt"]
    sizes.setCurrentIndex(sizes.findData(10.5))
    assert "五号" in sizes.currentText() and window.styles.valid
    assert window.styles.style.config.body.size_pt == 10.5
    block = window.styles.font_preview.document().firstBlock()
    sample_format = block.begin().fragment().charFormat()
    assert sample_format.fontPointSize() == 10.5
    assert sample_format.fontFamilies() == [family]
    window.request_preview()
    spin(lambda:window.latest_preview is not None and window.latest_preview.key == window.current_key())
    assert "font-size:10.5pt" in window.latest_preview.css
    target = tmp_path / "下拉字体字号.docx"
    window.start_export(OutputIntent.capture(target))
    spin(lambda:window.last_result is not None, timeout=30000)
    assert window.last_result.state == TaskState.SUCCEEDED
    normal = Document(target).styles["Normal"]
    assert normal.font.size.pt == 10.5
    assert normal.element.rPr.rFonts.get(qn("w:eastAsia")) == family


def test_typography_custom_sizes_and_template_reload(window):
    from md2word.ui.typography import SizePicker
    panel = window.styles
    panel.fields["body.size_pt"].setEditText("小四")
    assert panel.valid and panel.style.config.body.size_pt == 12
    panel.fields["body.size_pt"].setEditText("13.5 pt")
    panel.fields["headings.0.size_pt"].setEditText("小二")
    assert panel.valid and panel.style.config.headings[0].size_pt == 18
    saved = panel.style
    panel.restore()
    panel.load(saved)
    assert panel.fields["body.size_pt"].points() == 13.5
    assert panel.fields["headings.0.size_pt"].points() == 18
    assert all(isinstance(widget, SizePicker) for key, widget in panel.fields.items() if key.endswith(".size_pt"))
    for value in ("5.5", "49", "Infinity"):
        panel.fields["body.size_pt"].setEditText(value)
        assert not panel.valid and panel.style == saved
    panel.fields["body.size_pt"].setCurrentIndex(panel.fields["body.size_pt"].findData(14.0))
    assert panel.valid and panel.style.config.body.size_pt == 14


def test_utf16_issue_jump_after_emoji_and_crlf(window):
    source = '开头\U0001f600\r\n\r\n'+chr(36)+'x^2'+chr(36)+'\r\n'
    window.replace_session(DocumentSession.create(source))
    service = window.preview_lane.service
    result = service.render(request(service, window.session), threading.Event())
    location = next(loc for loc in result.document.source_map if loc.span and chr(36)+'x^2'+chr(36) in source[loc.span.start:loc.span.end])
    from md2word.domain.models import Issue, Severity
    issue = Issue("TEST_LOCATION", Severity.WARNING, "math", "公式检查", "检查公式", location.node_id, location)
    window.issues.show_issues([issue])
    window.jump_issue(window.issues.tree.topLevelItem(0))
    assert chr(36)+'x^2'+chr(36) in window.editor.textCursor().selectedText()
    window.editor.insertPlainText("changed")
    with pytest.raises(DocumentError):
        qt_selection(window.session.snapshot(), location)


def test_f13_old_result_cannot_overwrite_new_content_or_style(window):
    service = window.preview_lane.service
    window.editor.setPlainText("# 旧内容\n\n12345")
    window.debounce.stop()
    old = service.render(request(service, window.session, window.styles.style), threading.Event())
    window.editor.setPlainText("# 新内容\n\n99999")
    window.styles.fields["body.size_pt"].setEditText("14")
    window.debounce.stop()
    window.preview_lane.poll = lambda:old
    window.poll()
    assert window.latest_preview is None and window.preview.queued is None
    window.preview_lane.poll = lambda:None
    window.request_preview()
    # Restore actual poll after deliberate late-delivery injection.
    window.preview_lane.poll = PreviewLane.poll.__get__(window.preview_lane)
    spin(lambda:window.latest_preview is not None)
    assert window.latest_preview.key == window.current_key()
    assert "99999" in window.latest_preview.html and "12345" not in window.latest_preview.html


def test_f14_real_qt_edit_during_export_preserves_click_snapshot(window, tmp_path):
    window.editor.setPlainText("# 点击时版本\n\n原数字 12345\n")
    window.start_export(OutputIntent.capture(tmp_path / "中文 讲义.docx"))
    window.editor.setPlainText("# 编辑后的版本\n\n新数字 99999\n")
    spin(lambda:window.last_result is not None, timeout=30000)
    assert window.last_result.state == TaskState.SUCCEEDED
    assert window.session.dirty and "此前版本" not in window.status.text()  # Explicit revision message below.
    assert "当前编辑未导出" in window.status.text()
    with zipfile.ZipFile(tmp_path / "中文 讲义.docx") as archive:
        xml = archive.read("word/document.xml").decode()
    assert "12345" in xml and "99999" not in xml


def test_output_conflict_can_retry_verified_artifact_in_same_ui(window, tmp_path):
    target = tmp_path / "已存在.docx"
    target.write_bytes(b"existing original")
    window.editor.setPlainText("# 另存测试\n\n内容 24680")
    window.start_export(OutputIntent.capture(target, overwrite=True))
    spin(lambda:window.last_result is not None, timeout=30000)
    failed = window.last_result
    assert failed.state == TaskState.FAILED and failed.recovery_id
    assert target.read_bytes() == b"existing original"
    other = tmp_path / "新的讲义.docx"
    window.handle = window.scheduler.retry(failed, OutputIntent.capture(other))
    spin(lambda:window.last_result is not failed, timeout=30000)
    assert window.last_result.state == TaskState.SUCCEEDED and other.is_file()


def test_small_window_tabs_and_source_import_preservation(window, tmp_path):
    source = tmp_path / "中文 带空格.md"
    source.write_bytes('# 标题\r\n\r\n原文'.encode("utf-8"))
    assert window.import_path(source)
    assert window.session.current_text == '# 标题\r\n\r\n原文'
    window.resize(720, 680)
    QTest.qWait(100)
    assert window.width() <= 740 and window.compact and window.tabs.count() == 2
    assert not window.styles.isVisible() and window.more.isVisible()
    window.resize(1440, 960)
    QTest.qWait(100)
    assert not window.compact and window.columns.count() == 2
    assert window.editor_panel.isVisible() and window.preview.isVisible()


def test_real_webengine_local_math_gsap_and_hostile_source(window):
    source = '# 本地预览\n\n'+chr(36)+'x^2+1'+chr(36)+'\n\n<script>window.pwned=1</script>\n\n![图](https://example.com/a.png)\n'
    window.editor.setPlainText(source)
    window.request_preview()
    spin(lambda:window.latest_preview is not None)
    spin(lambda:bool(js_value(window, "window.previewState.ready")), timeout=20000)
    state = json.loads(js_value(window, "JSON.stringify(window.previewState)"))
    assert state["mathCount"] == 1 and state["gsap"] == "3.13.0" and not state["errors"]
    assert js_value(window, "String(window.pwned)") == "undefined"
    assert js_value(window, "document.querySelectorAll('#paper script,#paper img').length") == 0
    assert js_value(window, "window.qt === undefined") is True
    assert not window.preview.page.errors
    assert "mathjax/tex-chtml.js" in window.preview.policy.allowed
    assert any(name.endswith('.woff') for name in window.preview.policy.allowed)
    window.motion.setChecked(False)
    assert js_value(window, "window.motionEnabled") is False
    width = js_value(window, "document.documentElement.scrollWidth-innerWidth")
    assert width <= 2


def test_new_empty_session_clears_old_preview(window):
    window.editor.setPlainText("# 旧资料\n\n12345")
    window.request_preview()
    spin(lambda:window.latest_preview is not None)
    window.new_document()
    spin(lambda:bool(js_value(window, "window.previewState.ready")), timeout=15000)
    assert "12345" not in js_value(window, "document.getElementById('paper').textContent")
    assert not window.session.current_text


def test_first_paste_original_remains_after_qt_edits(window):
    from PySide6.QtCore import QMimeData
    source = "# 原始输入\r\n\r\n保留原文"
    mime = QMimeData()
    mime.setText(source)
    window.editor.insertFromMimeData(mime)
    assert window.session.original_text == source
    window.editor.insertPlainText("编辑")
    assert window.session.original_text == source


def test_invalid_style_corrected_to_previous_value_refreshes(window):
    window.editor.setPlainText("# 标题\n\n正文")
    window.request_preview()
    spin(lambda:window.latest_preview is not None)
    field = window.styles.fields["body.size_pt"]
    old = field.currentText()
    field.setEditText("invalid")
    assert not window.styles.valid
    field.setEditText(old)
    assert window.styles.valid and window.debounce.isActive()


def test_preview_failure_preserves_last_valid_document(window):
    window.editor.setPlainText("# 有效文档\n\n12345")
    window.request_preview()
    spin(lambda:window.latest_preview is not None)
    old = window.latest_preview
    # Real size guard, no truncation or fabricated successful parse.
    window.editor.setPlainText("测" * 700000)
    window.request_preview()
    spin(lambda:"预览未更新" in window.status.text())
    assert window.latest_preview is old and len(window.session.current_text) == 700000
    assert any(i.code == "INPUT_TOO_LARGE" for i in window.preview_issues)


def test_actual_resource_attacks_are_blocked_and_navigation_uses_browser(window, tmp_path, monkeypatch):
    sentinel = tmp_path / "非预览资源.js"
    sentinel.write_text("window.localLeak = true", "utf-8")
    url = QUrl.fromLocalFile(str(sentinel)).toString()
    window.preview.page.runJavaScript("window.attackDone=false;const probe=document.createElement('script');probe.src=" + json.dumps(url) + ";probe.onload=probe.onerror=()=>window.attackDone=true;document.head.appendChild(probe);")
    spin(lambda:bool(js_value(window,"window.attackDone")))
    assert js_value(window,"window.localLeak === undefined") is True
    window.preview.page.runJavaScript("window.networkBlocked=false;fetch('https://example.com/resource').catch(()=>window.networkBlocked=true)")
    spin(lambda:bool(js_value(window,"window.networkBlocked")))
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda value:opened.append(value.toString()) or True)
    kind = QWebEnginePage.NavigationType.NavigationTypeLinkClicked
    assert not window.preview.page.acceptNavigationRequest(QUrl("https://example.com/docs"),kind,True)
    assert opened == ["https://example.com/docs"]
    for target in ["file:///C:/blocked.txt", "javascript:alert(1)", "data:text/html,unsafe"]:
        assert not window.preview.page.acceptNavigationRequest(QUrl(target),kind,True)
    assert len(opened) == 1 and window.preview.page.createWindow(None) is None


def test_matching_export_math_fallback_updates_current_preview_only(window, tmp_path):
    window.editor.setPlainText("# 公式降级\n\n"+chr(36)+r"\unknowncommand{x}"+chr(36))
    window.start_export(OutputIntent.capture(tmp_path / "公式.docx"))
    spin(lambda:window.last_result is not None, timeout=30000)
    assert window.last_result.state == TaskState.SUCCEEDED
    assert window.math_fallbacks
    spin(lambda:window.latest_preview is not None and window.latest_preview.key.fallbacks)
    assert window.latest_preview.math_unchecked == 0 and "<code>" in window.latest_preview.html
    window.editor.insertPlainText("新版本")
    assert not window.math_fallbacks


def test_cancel_and_close_wait_for_worker_without_blocking_ui(window, tmp_path):
    window.editor.setPlainText("# 取消验证\n\n正文")
    window.start_export(OutputIntent.capture(tmp_path / "取消结果.docx"))
    window.cancel_export()
    spin(lambda:window.last_result is not None, timeout=30000)
    assert window.last_result.state == TaskState.CANCELLED
    assert not (tmp_path / "取消结果.docx").exists()


def test_replacement_guard_and_import_errors_keep_current_input(window, tmp_path, monkeypatch):
    window.editor.setPlainText("我当前的未保存内容")
    window.may_replace = MainWindow.may_replace.__get__(window)
    monkeypatch.setattr(QMessageBox,"question",lambda *a,**kw:QMessageBox.StandardButton.No)
    window.new_button.click()
    window.example_button.click()
    assert window.session.current_text == "我当前的未保存内容"
    incoming = tmp_path / "拒绝替换.md"
    incoming.write_text("# 不覆盖当前编辑",encoding="utf-8")
    assert not window.import_path(incoming)
    assert window.session.current_text == "我当前的未保存内容"
    bad = tmp_path / "编码错误.txt"
    bad.write_bytes(bytes([255,254,128]))
    with pytest.raises(DocumentError) as exc:
        window.import_path(bad)
    assert exc.value.code == "INPUT_ENCODING" and window.session.current_text == "我当前的未保存内容"
    window.may_replace = lambda:True
