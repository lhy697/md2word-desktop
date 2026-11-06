"""Review 4 regressions: real Win32, Pandoc/DOCX and Qt WebEngine behavior."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import replace
from html.parser import HTMLParser
import json
import os
import threading
import zipfile
from xml.etree import ElementTree as ET

import pytest
from md2word.domain.models import DocumentSession, InputMode, FrozenAst
from md2word.domain.ast_index import at_path
from md2word.domain.content_manifest import build_manifest
from md2word.domain.normalizer import normalize
from md2word.exporting.models import ExportSnapshot, TaskContext, TaskState
from md2word.exporting.output import OutputIntent
from md2word.exporting.service import ExportService
from md2word.live_preview import PreviewService
from md2word.parsing import Parser
from md2word.templates import fonts
from md2word.templates.service import resolve_style
from test_stage6 import app, window, spin, js_value, request


class HtmlText(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.feed(html)

    def handle_data(self, data):
        self.text += data


def test_nested_image_attributes_in_supported_ast_are_preserved():
    # The locked Markdown reader disables attribute syntax. Inject valid AST
    # attributes explicitly to cover the normalization contract without claiming
    # that the reader recognizes an additional Markdown extension.
    parsed = Parser().parse(DocumentSession.create('[![badge](https://example.org/badge.png "image title")](./report.pdf)').snapshot())
    ast = parsed.ast.to_dict()
    image = next(info for info in parsed.node_index if info.node_type == "Image")
    at_path(ast, image.path)["c"][0] = ["badge-id", ["stamp"], [["data-source", "original-value"]]]
    link = next(info for info in parsed.node_index if info.node_type == "Link")
    at_path(ast, link.path)["c"][0] = ["parent-id", ["reference"], [["title-kind", "outer"]]]
    parsed = replace(parsed, ast=FrozenAst.capture(ast), manifest=build_manifest(ast))
    result = normalize(parsed)
    assert result.exportable
    text = "".join(entry.payload["text"] for entry in result.manifest.of_kind("Code"))
    for value in ["badge-id", "stamp", "data-source", "original-value", "parent-id", "reference", "title-kind", "outer"]:
        assert value in text


@pytest.mark.skipif(os.name != "nt", reason="Real Win32 font enumeration regression")
def test_parallel_native_font_enumeration_uses_compatible_signatures():
    barrier = threading.Barrier(2)
    def enumerate_many(_):
        inventories, errors = [], []
        for iteration in range(200):
            barrier.wait(timeout=10)
            try:
                inventories.append(fonts.system_fonts())
            except Exception as exc:
                errors.append((iteration, type(exc).__name__, str(exc)))
        return inventories, errors
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enumerate_many, (0, 1)))
    assert not [error for _, errors in results for error in errors]
    assert all(inventory for inventories, _ in results for inventory in inventories)


@pytest.mark.skipif(os.name != "nt", reason="Real device-context cleanup with injected enumeration failure")
@pytest.mark.parametrize("error", [OSError, ctypes.ArgumentError])
def test_font_inventory_failure_is_advisory_and_releases_dc(error, monkeypatch, tmp_path):
    get_dc, release_dc, _ = fonts._font_api()
    released = []
    def failing_enum(*args):
        raise error("explicit font API fault injection")
    def release(origin, dc):
        released.append(dc)
        return release_dc(origin, dc)
    monkeypatch.setattr(fonts, "_font_api", lambda:(get_dc, release, failing_enum))
    report = fonts.check_fonts(resolve_style())
    assert [issue.code for issue in report.issues] == ["FONT_CHECK_UNAVAILABLE"]
    session = DocumentSession.create("# 字体检查不可用\n\n转换仍应完成")
    preview = PreviewService()
    rendered = preview.render(request(preview, session), threading.Event())
    assert rendered.failure is None
    assert "FONT_CHECK_UNAVAILABLE" in {issue.code for issue in rendered.issues}
    service = ExportService(work_root=tmp_path / "jobs")
    try:
        exported = service.run(ExportSnapshot.capture(session, resolve_style()),
                               OutputIntent.capture(tmp_path / "font-advisory.docx"), TaskContext())
        assert exported.state == TaskState.SUCCEEDED
        assert "FONT_CHECK_UNAVAILABLE" in {issue.code for issue in exported.issues}
    finally:
        service.close()
    assert len(released) == 3 and all(released)


@pytest.mark.parametrize("source", [
    '![badge](https://example.org/badge.png "image title")',
    '[![badge](https://example.org/badge.png "image title")](./report.pdf)',
    '[**![badge](https://example.org/badge.png "image title")**](./report.pdf)',
    '["![badge](https://example.org/badge.png "image title")"](./report.pdf)',
    '[![badge](https://example.org/badge.png "image title"){#badge .stamp width=12}](./report.pdf)',
    '[![first](https://example.org/first.png "first title") and ![second](https://example.org/second.png "second title")](./report.pdf)',
])
def test_original_image_manifest_references_survive_preview_and_docx(source, tmp_path):
    session = DocumentSession.create(source)
    parsed = Parser().parse(session.snapshot())
    images = parsed.manifest.of_kind("Image")
    assert images
    preview = PreviewService()
    rendered = preview.render(request(preview, session), threading.Event())
    assert rendered.failure is None
    service = ExportService(work_root=tmp_path / "jobs")
    try:
        result = service.run(ExportSnapshot.capture(session, resolve_style()),
                             OutputIntent.capture(tmp_path / "nested.docx"), TaskContext())
        assert result.state == TaskState.SUCCEEDED
        with zipfile.ZipFile(result.output_path) as archive:
            document = ET.fromstring(archive.read("word/document.xml"))
        text = ''.join(node.text or '' for node in document.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
        # Expectations come directly from the original parsed manifest, never from
        # fallback_for/rich_description or a replay of the replacement algorithm.
        for actual in (HtmlText(rendered.html).text, text):
            for image in images:
                payload = image.payload
                for field in ("text", "target", "title"):
                    assert payload[field] in actual
                identity, classes, pairs = payload["attributes"]
                for value in [identity, *classes, *(v for pair in pairs for v in pair)]:
                    assert value in actual
            if "./report.pdf" in source:
                assert "./report.pdf" in actual
        image_issues = [issue for issue in result.issues if issue.code == "IMAGE_NOT_SUPPORTED"]
        assert {issue.node_id for issue in image_issues} == {image.entry_id for image in images}
    finally:
        service.close()


@pytest.mark.parametrize("mode,source,expected_math,literals", [
    (InputMode.PLAIN, r'纯文本应保留：\(x^2\) 与 \[y=1\]', 0, [r'\(x^2\)', r'\[y=1\]']),
    (InputMode.MARKDOWN, r'普通字面文字：\\(x^2\\) 与 \\[y=1\\]', 0, [r'\(x^2\)', r'\[y=1\]']),
    (InputMode.MARKDOWN, r'字面 \\(x^2\\)；真的 $a^2$ 与 $$b=2$$', 2, [r'\(x^2\)']),
    (InputMode.MARKDOWN, r'`\(code\)`、链接 [\\(label\\)](https://example.org) 和 $z$', 1, [r'\(code\)', r'\(label\)']),
    (InputMode.MARKDOWN, '```{.math .inline}\n'+r'\(literal code\)'+'\n```', 0, [r'\(literal code\)']),
])
def test_only_ast_math_nodes_are_typeset_in_actual_webengine(window, mode, source, expected_math, literals):
    window.replace_session(DocumentSession.create(source, mode))
    window.request_preview()
    spin(lambda: window.latest_preview is not None)
    spin(lambda: bool(js_value(window, "window.previewState.ready")))
    state = json.loads(js_value(window, "JSON.stringify(window.previewState)"))
    assert window.latest_preview.math_unchecked == expected_math
    assert state["mathCount"] == expected_math
    assert state["errors"] == 0
    text = js_value(window, "document.getElementById('paper').textContent")
    for literal in literals:
        assert literal in text


def test_math_shaped_plain_text_exports_without_omml(window, tmp_path):
    source = r'纯文本应保留：\(x^2\) 与 \[y=1\]'
    window.replace_session(DocumentSession.create(source, InputMode.PLAIN))
    window.request_preview()
    spin(lambda: window.latest_preview is not None)
    spin(lambda: bool(js_value(window, "window.previewState.ready")))
    assert js_value(window, "document.querySelectorAll('#paper mjx-container').length") == 0
    window.start_export(OutputIntent.capture(tmp_path / "plain.docx"))
    spin(lambda: window.last_result is not None)
    assert window.last_result.state == TaskState.SUCCEEDED
    with zipfile.ZipFile(window.last_result.output_path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
    assert not document.findall('.//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath')
    assert ''.join(node.text or '' for node in document.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')) == source


def test_gui_verification_marks_failure_if_later_case_raises(app, tmp_path, monkeypatch):
    from md2word.ui.main_window import MainWindow
    from md2word.ui.verification import verify_gui
    original = MainWindow.replace_session
    def injected_failure(self, session, title="未命名文档"):
        if session.input_mode == InputMode.PLAIN:
            raise RuntimeError("explicit late verification fault")
        return original(self, session, title)
    monkeypatch.setattr(MainWindow, "replace_session", injected_failure)
    output = tmp_path / "verification-late-failure"
    assert verify_gui(output) == 1
    report = json.loads((output / "verification.json").read_text("utf-8"))
    assert report["example_export_state"] == "succeeded"
    assert report["click_snapshot_preserved"]
    assert report["passed"] is False and report["error"] == "RuntimeError"
