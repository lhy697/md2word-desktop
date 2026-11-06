"""Local selection formatting must survive the complete preview/Word/draft path."""
from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from md2word.domain.local_formatting import CharacterFormat,ParagraphFormat,apply_selection,resolve_formats,merge_runs
from md2word.domain.models import DocumentSession,InputMode,Issue,Severity
from md2word.exporting.models import ExportSnapshot,TaskContext,TaskState
from md2word.exporting.output import OutputIntent
from md2word.exporting.service import ExportService
from md2word.exporting.scheduler import Scheduler
from md2word.live_preview import PreviewService,PreviewRequest,PreviewKey
from md2word.templates.service import resolve_style,PersonalStyleRepository
from md2word.conversion.ooxml import Package,NS,W,M,q
from md2word.storage.files import UserPaths
from md2word.storage.models import Draft
from md2word.storage.repositories import DraftRepository
from md2word.ui.main_window import MainWindow
from md2word.ui.persistence import PersistenceController
from test_stage6 import app,spin,js_value


def preview(session,service=None,style=None):
    service = service or PreviewService()
    style = style or resolve_style()
    key = PreviewKey(session.session_id,session.content_revision,session.style_revision,service.parser.profile.fingerprint,
                     style.resolved_hash,style.template_version,(),session.local_hash)
    result = service.render(PreviewRequest(key,session.snapshot(),style,session.local_formats),threading.Event())
    assert result.failure is None
    return result


def formatted(session,needle,character=None,paragraph=None,index=0):
    result = preview(session)
    matches = [(i,t) for i,t in enumerate(result.local_targets) if needle in t.text]
    token,target = matches[index]
    start = target.text.index(needle)
    values = apply_selection(result.document,session.local_formats,[{"block":f"lf{token}","start":start,"end":start+len(needle)}],
                             character,paragraph)
    return session.format_selection(values)


def export(session,tmp_path):
    service = ExportService(work_root=tmp_path / "tasks")
    try:
        target = tmp_path / "local.docx"
        result = service.run(ExportSnapshot.capture(session,resolve_style()),OutputIntent.capture(target),TaskContext())
        assert result.state == TaskState.SUCCEEDED, result.failure
        return Package.read(target)
    finally:
        service.close()


def run_formats(paragraph):
    result = []
    for run in paragraph.findall(".//w:r",NS):
        if any(n.tag == q(M,"oMath") for n in run.iterancestors()):
            continue
        text = "".join(node.text or "" for node in run.findall("w:t",NS))
        result.append((text,run.find(q(W,"rPr"))))
    return result


def test_partial_fonts_and_paragraph_spacing_survive_real_docx_without_losing_math_or_links(tmp_path):
    session = DocumentSession.create("第一段😀 alpha **bold** [链接](https://example.org/a \"title\") $x^2$ 结束。\n\n第二段保持默认。")
    session = formatted(session,"alpha bold 链接",CharacterFormat(font_zh="黑体",font_latin="Arial",size_pt=16.,letter_spacing_pt=1.2,
                        bold=True,italic=True,underline=True),ParagraphFormat(line_spacing={"mode":"multiple","multiplier":2.},before_pt=6.,after_pt=9.,alignment="center"))
    package = export(session,tmp_path)
    paragraphs = package.document.findall(".//w:body/w:p",NS)
    first = run_formats(paragraphs[0])
    selected = [(t,p) for t,p in first if t and ("alpha" in t or "bold" in t or "链接" in t)]
    assert selected
    for text,prop in selected:
        assert prop.find(q(W,"rFonts")).get(q(W,"ascii")) == "Arial"
        assert prop.find(q(W,"rFonts")).get(q(W,"eastAsia")) == "黑体"
        assert prop.find(q(W,"sz")).get(q(W,"val")) == "32"
        assert prop.find(q(W,"spacing")).get(q(W,"val")) == "24"
        assert prop.find(q(W,"b")).get(q(W,"val")) == "1"
    assert first[0][1].find(q(W,"sz")).get(q(W,"val")) == "24"
    spacing = paragraphs[0].find("w:pPr/w:spacing",NS)
    assert spacing.get(q(W,"line")) == "480" and spacing.get(q(W,"before")) == "120" and spacing.get(q(W,"after")) == "180"
    assert paragraphs[0].find("w:pPr/w:jc",NS).get(q(W,"val")) == "center"
    assert len(package.document.findall(".//m:oMath",NS)) == 1
    assert package.hyperlink(package.document.find(".//w:hyperlink",NS))[0] == "https://example.org/a"
    assert run_formats(paragraphs[1])[0][1].find(q(W,"sz")).get(q(W,"val")) == "24"


def test_repeated_paragraph_selection_changes_only_the_chosen_occurrence(tmp_path):
    session = DocumentSession.create("重复 alpha\n\n重复 alpha")
    session = formatted(session,"alpha",CharacterFormat(size_pt=18.),index=1)
    package = export(session,tmp_path)
    paragraphs = package.document.findall(".//w:body/w:p",NS)
    sizes = [[p.find(q(W,"sz")).get(q(W,"val")) for t,p in run_formats(para) if "alpha" in t] for para in paragraphs]
    assert sizes == [["24"],["36"]]


@pytest.mark.parametrize("source,needle",[("| A | B |\n|---|---|\n| 单元格 alpha | 保持 |","alpha"),
    ("```text\nline1\nline2 alpha\tend\n```","alpha"),("3. alpha\n4. unchanged","alpha"),
    ("prefix $x^2$ alpha\n\n$$y=1$$","alpha"),
    (r"prefix $\unknowncommand{x}$ alpha","alpha")])
def test_local_runs_preserve_tables_code_numbering_and_math_fallbacks(tmp_path,source,needle):
    session = formatted(DocumentSession.create(source),needle,CharacterFormat(size_pt=14.))
    package = export(session,tmp_path)
    assert "alpha" in "".join(n.text or "" for n in package.document.findall(".//w:t",NS))


def test_overlapping_formats_merge_and_clear_only_the_selected_characters():
    session = DocumentSession.create("abcdefghij")
    session = formatted(session,"bcdef",CharacterFormat(size_pt=16.,bold=True))
    session = formatted(session,"defgh",CharacterFormat(italic=True))
    runs = session.local_formats[0].runs
    assert [(r.start,r.end) for r in runs] == [(1,3),(3,6),(6,8)]
    result = preview(session)
    values = apply_selection(result.document,session.local_formats,[{"block":"lf0","start":4,"end":5}],clear=True)
    assert all(not (r.start <= 4 < r.end) for r in values[0].runs)
    assert any(r.format.size_pt == 16 for r in values[0].runs)


def test_unique_paragraph_format_rebases_after_insertion_and_is_saved_in_draft(tmp_path):
    session = formatted(DocumentSession.create("first\n\nunique alpha\n\nlast"),"alpha",CharacterFormat(size_pt=18.))
    changed = session.edit("inserted\n\nfirst\n\nunique alpha\n\nlast")
    result = preview(changed)
    assert not result.local_unresolved and result.local_resolved[0].node_id != session.local_formats[0].node_id
    package = export(changed,tmp_path)
    assert any(p.find(q(W,"sz")).get(q(W,"val")) == "36" for t,p in run_formats(package.document.findall(".//w:body/w:p",NS)[2]) if "alpha" in t)
    paths = UserPaths(tmp_path / "state")
    repo = DraftRepository(paths.drafts)
    repo.save(Draft.capture(changed,resolve_style(),"format"))
    assert repo.load().value.session() == changed


def test_ambiguous_rebase_blocks_export_instead_of_formatting_wrong_paragraph(tmp_path):
    session = formatted(DocumentSession.create("repeat alpha\n\nrepeat alpha"),"alpha",CharacterFormat(size_pt=18.),index=1)
    changed = session.edit("inserted\n\nrepeat alpha\n\nrepeat alpha")
    result = preview(changed)
    assert result.local_unresolved and any(i.code == "LOCAL_FORMAT_STALE" for i in result.issues)
    service = ExportService(work_root=tmp_path / "tasks")
    try:
        output = service.run(ExportSnapshot.capture(changed,resolve_style()),OutputIntent.capture(tmp_path / "blocked.docx"),TaskContext())
        assert output.state == TaskState.FAILED and output.failure.code == "LOCAL_FORMAT_STALE"
        assert not (tmp_path / "blocked.docx").exists()
    finally:
        service.close()


def test_v1_draft_migrates_to_v2_with_exact_backup(tmp_path):
    paths = UserPaths(tmp_path / "state")
    repo = DraftRepository(paths.drafts)
    raw = Draft.capture(DocumentSession.create("old v1"),resolve_style(),"old").model_dump(mode="json")
    raw["schema_version"] = 1
    raw.pop("local_formats")
    original = json.dumps(raw,ensure_ascii=False).encode("utf-8")
    repo.path.write_bytes(original)
    loaded = repo.load().value
    assert loaded.schema_version == 2 and loaded.local_formats == ()
    repo.save(loaded)
    assert (paths.drafts / "session.v1.json").read_bytes() == original
    repo.clear()
    assert not list(paths.drafts.glob("*.json"))


@pytest.fixture
def ui(app,tmp_path,monkeypatch):
    monkeypatch.setattr(PersistenceController,"offer_recovery",lambda self,*_:None)
    paths = UserPaths(tmp_path / "state")
    w = MainWindow(user_paths=paths,style_repository=PersonalStyleRepository(paths.styles),
                   scheduler=Scheduler(ExportService(work_root=paths.tasks)))
    w.may_replace = lambda:True
    w.show()
    spin(lambda:w.preview.loaded)
    w.motion.setChecked(False)
    yield w
    if not w.disposed:
        w.persistence.close_choice = lambda:"discard"
        w.persistence.close_failure_choice = lambda:"discard"
        w.close()
        spin(lambda:w.disposed)
    app.processEvents()


def ready(w):
    spin(lambda:w.latest_preview is not None and w.latest_preview.key == w.current_key())
    spin(lambda:bool(js_value(w,"window.previewState.ready")))


def test_real_preview_selection_applies_local_format_and_can_undo_and_export(ui,tmp_path):
    w = ui
    w.editor.setPlainText("before 😀 alpha after")
    w.request_preview()
    ready(w)
    w.style_tabs.setCurrentIndex(1)
    js_value(w,"""(()=>{const leaf=Array.from(document.querySelectorAll('span[data-lf-start]')).find(e=>e.textContent==='alpha');
        const range=document.createRange();range.selectNodeContents(leaf);const s=getSelection();s.removeAllRanges();s.addRange(range);return true;})()""")
    spin(lambda:bool(js_value(w,"window.getLocalSelection()!==null")))
    w.local_panel.size.setCurrentIndex(w.local_panel.size.findData(18.))
    w.local_panel.font_zh.setCurrentText("黑体")
    w.local_panel.before.setValue(6)
    QTest.mouseClick(w.local_panel.apply_button,Qt.MouseButton.LeftButton)
    spin(lambda:bool(w.session.local_formats))
    ready(w)
    value = js_value(w,"""(()=>{const leaf=Array.from(document.querySelectorAll('span[data-lf-start]')).find(e=>e.textContent==='alpha');return getComputedStyle(leaf).fontSize;})()""")
    assert float(value.removesuffix("px")) == pytest.approx(24.)
    assert "alpha" in js_value(w,"getSelection().toString()")
    assert w.session.current_text == "before 😀 alpha after"
    w.local_panel.undo()
    spin(lambda:not w.session.local_formats)
    ready(w)
    w.local_panel.redo()
    ready(w)
    w.start_export(OutputIntent.capture(tmp_path / "ui.docx"))
    spin(lambda:w.last_result is not None,timeout=30000)
    assert w.last_result.state == TaskState.SUCCEEDED


def test_native_window_frame_minimize_restore_and_close(ui):
    w = ui
    assert not hasattr(w, "window_controls")
    assert w.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
    assert w.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    assert w.windowFlags() & Qt.WindowType.WindowCloseButtonHint
    w.showMinimized()
    spin(lambda:w.isMinimized())
    w.showNormal()
    w.showMaximized()
    spin(lambda:w.isMaximized())
    w.showNormal()
    spin(lambda:not w.isMaximized())
    w.close()
    spin(lambda:w.disposed)


def test_issue_details_reclaim_space_and_only_expand_for_new_errors(ui):
    w, panel = ui, ui.issues
    assert not panel.toggle.isChecked() and not panel.tree.isVisible()
    QApplication.processEvents()
    collapsed_height = w.horizontal.height()
    warning = Issue("TEST_WARNING", Severity.WARNING, "test", "测试警告", "查看建议")
    error = Issue("TEST_ERROR", Severity.BLOCKING, "test", "测试错误", "修正输入")
    panel.show_issues((warning,))
    assert not panel.tree.isVisible()
    panel.toggle.click()
    QApplication.processEvents()
    assert panel.tree.isVisible() and w.horizontal.height() < collapsed_height - 50
    panel.toggle.click()
    panel.show_issues((error,))
    QApplication.processEvents()
    assert panel.tree.isVisible()
    panel.toggle.click()
    panel.show_issues((error, warning))
    assert not panel.tree.isVisible()  # A dismissed error must not reopen on every poll.
    panel.show_issues((), checked=False)
    panel.show_issues((error,))
    assert not panel.tree.isVisible()  # Editing invalidates preview without forgetting dismissal.
    panel.show_issues(())
    panel.show_issues((error,))
    assert panel.tree.isVisible()  # Resolved, then newly detected again.
    panel.show_issues(())
    QApplication.processEvents()
    assert not panel.tree.isVisible() and w.horizontal.height() >= collapsed_height


def test_issue_auto_expand_opt_out_persists_and_manual_open_remains_available(ui):
    w, panel = ui, ui.issues
    panel.auto_expand.setChecked(False)
    stored = w.persistence.preferences.load().value
    assert stored is not None and stored.auto_expand_errors is False
    error = Issue("TEST_ERROR", Severity.BLOCKING, "test", "测试错误", "修正输入")
    panel.show_issues((error,))
    assert not panel.tree.isVisible() and "1 项阻断" in panel.toggle.text()
    panel.toggle.click()
    assert panel.tree.isVisible()
    panel.toggle.click()
    panel.auto_expand.setChecked(True)
    panel.show_issues((error,))
    assert not panel.tree.isVisible()
    another = Issue("TEST_ERROR_2", Severity.BLOCKING, "test", "另一错误", "修正输入")
    panel.show_issues((error, another))
    assert panel.tree.isVisible()


def test_local_formats_survive_draft_recovery(ui):
    w = ui
    w.editor.setPlainText("persistent alpha")
    w.request_preview()
    ready(w)
    value = apply_selection(w.latest_preview.document,(),[{"block":"lf0","start":11,"end":16}],CharacterFormat(size_pt=16.))
    w.local_panel.commit(value)
    ready(w)
    expected = w.session
    p = w.persistence
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    w.replace_session(DocumentSession.create("replacement"))
    p.recovery = p.repository.load()
    p.recovery_pending = True
    p.restore_draft()
    assert w.session == expected
    ready(w)
    assert w.session.local_formats[0].runs[0].format.size_pt == 16.


def test_source_undo_restores_formats_that_belonged_to_deleted_text(ui):
    w = ui
    w.editor.setPlainText("before alpha after")
    w.request_preview()
    ready(w)
    values = apply_selection(w.latest_preview.document,(),[{"block":"lf0","start":7,"end":12}],CharacterFormat(size_pt=18.))
    w.local_panel.commit(values)
    ready(w)
    cursor = w.editor.textCursor()
    cursor.setPosition(7)
    cursor.setPosition(12,cursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    w.request_preview()
    ready(w)
    assert not w.session.local_formats
    w.editor.undo()
    w.request_preview()
    ready(w)
    assert w.session.current_text == "before alpha after" and w.session.local_formats
    assert w.session.local_formats[0].runs[0].format.size_pt == 18.


def test_stale_browser_selection_cannot_apply_to_new_source(ui):
    w = ui
    w.editor.setPlainText("alpha")
    w.request_preview()
    ready(w)
    old_revision = w.preview.revision_token(w.current_key())
    w.editor.setPlainText("new document")
    w.request_preview()
    ready(w)
    js_value(w,"window.localSelection="+json.dumps({"revision":old_revision,"blocks":[{"block":"lf0","start":0,"end":5}]}))
    w.local_panel.read_selection()
    spin(lambda:w.local_panel.selection is None)
    assert not w.session.local_formats


def test_emoji_selection_uses_code_points_and_keeps_adjacent_text_unchanged(ui,tmp_path):
    w = ui
    w.editor.setPlainText("甲😀乙")
    w.request_preview()
    ready(w)
    js_value(w,"""(()=>{const leaf=document.querySelector('span[data-lf-start]');const n=leaf.firstChild;
        const r=document.createRange();r.setStart(n,1);r.setEnd(n,3);const s=getSelection();s.removeAllRanges();s.addRange(r);return true;})()""")
    spin(lambda:bool(js_value(w,"window.getLocalSelection()!==null")))
    selection = json.loads(js_value(w,"JSON.stringify(window.getLocalSelection())"))["blocks"]
    assert selection == [{"block":"lf0","start":1,"end":2}]
    w.local_panel.size.setCurrentIndex(w.local_panel.size.findData(18.))
    w.local_panel.apply_current(selection)
    ready(w)
    w.start_export(OutputIntent.capture(tmp_path / "emoji.docx"))
    spin(lambda:w.last_result is not None,timeout=30000)
    assert w.last_result.state == TaskState.SUCCEEDED
    runs = run_formats(Package.read(tmp_path / "emoji.docx").document.find(".//w:body/w:p",NS))
    assert [(text,prop.find(q(W,"sz")).get(q(W,"val"))) for text,prop in runs] == [("甲","24"),("😀","36"),("乙","24")]


def test_duplicate_heading_auto_ids_do_not_move_formatting_to_wrong_occurrence():
    session = formatted(DocumentSession.create("# Repeat\n\n# Repeat"),"Repeat",CharacterFormat(size_pt=18.),index=1)
    changed = session.edit("# Repeat\n\n# Repeat\n\n# Repeat")
    result = preview(changed)
    assert result.local_unresolved and not result.local_resolved


def test_inline_code_font_and_link_underline_render_the_requested_local_values(ui):
    w = ui
    w.editor.setPlainText("`alpha` [beta](https://example.org)")
    w.request_preview()
    ready(w)
    w.local_panel.font_latin.setCurrentText("Arial")
    w.local_panel.underline.setCheckState(Qt.CheckState.Unchecked)
    w.local_panel.apply_current([{"block":"lf0","start":0,"end":10}])
    ready(w)
    family = js_value(w,"getComputedStyle(document.querySelector('#paper code')).fontFamily")
    decoration = js_value(w,"getComputedStyle(document.querySelector('#paper a span[data-lf-start]')).textDecorationLine")
    assert "Arial" in family and decoration == "none"


def test_preview_failure_keeps_local_formats_available_for_source_undo(ui):
    w = ui
    w.editor.setPlainText("before alpha after")
    w.request_preview()
    ready(w)
    values = apply_selection(w.latest_preview.document,(),[{"block":"lf0","start":7,"end":12}],CharacterFormat(size_pt=18.))
    w.local_panel.commit(values)
    ready(w)
    old = w.session.local_formats
    w.editor.setPlainText("测"*700000)
    w.request_preview()
    spin(lambda:"预览未更新" in w.status.text())
    assert w.session.local_formats == old


@pytest.mark.parametrize("raw",[{"font_zh":""},{"font_latin":"a"*81},{"font_zh":"bad/font"},
    {"size_pt":13.4},{"letter_spacing_pt":float("nan")},{"size_pt":49.}])
def test_invalid_local_character_settings_are_rejected_before_preview_or_save(raw):
    with pytest.raises(ValueError):
        CharacterFormat.model_validate(raw)
