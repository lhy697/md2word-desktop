"""Review 5: deep JSON recovery and transactional draft clearing."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import threading

import pytest
from PySide6.QtTest import QTest
from md2word.domain.models import DocumentError, InputMode
from md2word.exporting.models import TaskState
from md2word.exporting.output import OutputIntent
from md2word.storage.models import Settings
from md2word.storage.repositories import DraftRepository, SettingsRepository
from test_stage7 import app, paths, state_window, draft, spin


DEEP_JSON = b'{"schema_version":1,"nested":' + b"[" * 30000 + b"0" + b"]" * 30000 + b"}"


@pytest.mark.parametrize("kind", ["draft", "settings"])
@pytest.mark.parametrize("damaged", ["primary", "backup", "both"])
def test_deep_json_is_recoverable_and_preserved(paths, kind, damaged):
    repo = DraftRepository(paths.drafts) if kind == "draft" else SettingsRepository(paths.settings)
    value = draft("valid backup") if kind == "draft" else Settings(motion=False)
    repo.save(value)
    repo.save(value)
    assert repo.load().value == value
    assert len(DEEP_JSON) < repo.limit
    affected = [repo.path] if damaged == "primary" else [repo.backup] if damaged == "backup" else [repo.path, repo.backup]
    for path in affected:
        path.write_bytes(DEEP_JSON)
    loaded = repo.load()
    assert all(path.read_bytes() == DEEP_JSON for path in affected)
    if damaged == "both":
        assert loaded.value is None and repo.blocked
        assert len(loaded.issues) == 2 and all(i.code.endswith("READ_FAILED") for i in loaded.issues)
        with pytest.raises(DocumentError):
            repo.save(value)
        assert all(path.read_bytes() == DEEP_JSON for path in affected)
    else:
        assert loaded.value == value and not repo.blocked
        if damaged == "primary":
            assert loaded.source == "上一次有效快照"
            assert any(i.code.endswith("READ_FAILED") for i in loaded.issues)
        repo.save(loaded.value)
        assert repo.load().value == value
        preserved = list(repo.directory.glob("*.preserved-*.json"))
        assert len(preserved) == 1 and preserved[0].read_bytes() == DEEP_JSON


@contextmanager
def deny_delete(path):
    """A real Windows sharing lock, not a mocked unlink/replace failure."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        assert kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Requires a real Windows sharing lock")
def test_failed_clear_keeps_latest_edit_and_close_save_or_cancel(state_window):
    w, p = state_window, state_window.persistence
    for text in ("previous backup", "previous saved draft"):
        w.editor.setPlainText(text)
        p.save_now()
        spin(lambda:p.lane.idle and p.saved_key == p.key())
    w.editor.setPlainText("LATEST EDIT MUST SURVIVE FAILED CLEAR")
    p.timer.stop()
    w.debounce.stop()
    choices = []
    p.close_failure_choice = lambda:(choices.append("cancel") or "cancel")
    with deny_delete(p.repository.backup):
        p.clear_saved()
        spin(lambda:p.clear_ticket is None and p.lane.idle)
        assert p.last_issue.code == "DRAFT_CLEAR_FAILED"
        assert p.cleared_key is None
        assert "未完全清空" in p.status.text()
        w.close()
        spin(lambda:bool(choices))
        assert not w.disposed and not p.close_requested and w.centralWidget().isEnabled()
        assert w.session.current_text == "LATEST EDIT MUST SURVIVE FAILED CLEAR"
    w.close()
    spin(lambda:w.disposed)
    assert DraftRepository(p.paths.drafts).load().value.current_text == "LATEST EDIT MUST SURVIVE FAILED CLEAR"


def test_clear_then_real_export_does_not_recreate_draft_even_when_autosave_toggled(state_window, tmp_path):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("cleared content stays cleared after export")
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    p.clear_saved()
    spin(lambda:p.clear_ticket is None and p.lane.idle)
    assert not list(p.paths.drafts.glob("*.json"))
    original = w.session.current_text
    w.start_export(OutputIntent.capture(tmp_path / "after-clear.docx"))
    spin(lambda:w.last_result is not None, timeout=30000)
    assert w.last_result.state == TaskState.SUCCEEDED and not w.session.dirty
    assert w.session.current_text == original
    p.auto_save.setChecked(False)
    assert "草稿已清空" in p.status.text()
    p.auto_save.setChecked(True)
    assert "继续编辑后自动保存" in p.status.text()
    QTest.qWait(p.SAVE_DELAY_MS + 150)
    assert p.lane.idle and not p.timer.isActive()
    assert not list(p.paths.drafts.glob("*.json"))
    w.close()
    spin(lambda:w.disposed)
    assert DraftRepository(p.paths.drafts).load().value is None


@pytest.mark.parametrize("edit", ["text", "mode", "style", "local"])
def test_real_document_edits_resume_autosave_after_successful_clear(state_window, edit):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("alpha beta")
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    p.clear_saved()
    spin(lambda:p.clear_ticket is None and p.lane.idle)
    if edit == "text":
        w.editor.insertPlainText("new ")
    elif edit == "mode":
        w.mode.setCurrentIndex(1)
        assert w.session.input_mode == InputMode.PLAIN
    elif edit == "style":
        w.styles.fields["body.size_pt"].setEditText("14")
    else:
        from md2word.domain.local_formatting import CharacterFormat, apply_selection
        from test_local_formatting import preview
        result = preview(w.session)
        formats = apply_selection(result.document, (), [{"block":"lf0", "start":0, "end":5}],
                                  CharacterFormat(size_pt=18))
        w.session = w.session.format_selection(formats)
        w.invalidate()
    spin(lambda:p.lane.idle and p.repository.path.exists() and p.saved_key == p.key())
    stored = p.repository.load().value
    assert stored.session() == w.session and stored.style() == w.styles.style
    assert p.cleared_key is None


def test_clear_is_only_committed_after_success_and_edits_during_clear_are_saved(state_window):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("before clear")
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    original = p.repository.clear
    entered, release = threading.Event(), threading.Event()
    def slow_clear():
        entered.set()
        assert release.wait(5)
        original()
    p.repository.clear = slow_clear
    try:
        p.clear_saved()
        assert entered.wait(2)
        assert p.cleared_key is None
        w.editor.setPlainText("edit while clear is in progress")
    finally:
        release.set()
    spin(lambda:p.clear_ticket is None and p.lane.idle and p.saved_key == p.key())
    assert p.repository.load().value.current_text == "edit while clear is in progress"


def test_manual_save_after_clear_is_an_explicit_new_snapshot(state_window):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("explicit save restores recovery")
    p.clear_saved()
    spin(lambda:p.clear_ticket is None and p.lane.idle)
    assert not p.repository.path.exists()
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    assert p.repository.load().value.current_text == "explicit save restores recovery"


def test_pending_clear_does_not_leave_an_autosave_timer_after_preference_toggle(state_window):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("unchanged while clearing")
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    original = p.repository.clear
    entered, release = threading.Event(), threading.Event()
    def slow_clear():
        entered.set()
        assert release.wait(5)
        original()
    p.repository.clear = slow_clear
    try:
        p.clear_saved()
        assert entered.wait(2)
        p.auto_save.setChecked(False)
        p.auto_save.setChecked(True)
    finally:
        release.set()
    spin(lambda:p.clear_ticket is None and p.lane.idle)
    assert not p.timer.isActive()
    QTest.qWait(p.SAVE_DELAY_MS + 150)
    assert p.lane.idle and not p.repository.path.exists()
