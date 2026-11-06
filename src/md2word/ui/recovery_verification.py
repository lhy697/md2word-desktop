"""Opt-in review 5 verification with isolated files and a real Windows lock."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from ..domain.models import DocumentSession
from ..exporting.models import TaskState
from ..exporting.output import OutputIntent
from ..exporting.scheduler import Scheduler
from ..exporting.service import ExportService
from ..storage.files import UserPaths
from ..storage.models import Draft, Settings
from ..storage.repositories import DraftRepository, SettingsRepository
from ..templates.service import PersonalStyleRepository, resolve_style
from .main_window import MainWindow
from .verification import wait_for


def verify_review5(directory, *, with_damaged_styles=False):
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = UserPaths(output / "isolated-review5-state")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    report = {"passed": False, "production_state_used": False, "fixed_sample": True}
    window = None
    timer = QTimer()
    prompts = []
    def handle_prompt():
        modal = QApplication.activeModalWidget()
        if not isinstance(modal, QMessageBox):
            return
        label = {"恢复本地草稿": "恢复草稿", "草稿保存失败": "取消关闭，保留内容"}.get(modal.windowTitle())
        if label:
            prompts.append(modal.windowTitle())
            next(button for button in modal.buttons() if button.text() == label).click()
    timer.timeout.connect(handle_prompt)
    timer.start(40)
    def open_window():
        result = MainWindow(user_paths=paths, style_repository=PersonalStyleRepository(paths.styles),
                            scheduler=Scheduler(ExportService(work_root=paths.tasks)))
        result.may_replace = lambda: True
        result.show()
        return result
    try:
        report["phase"] = "startup with deep JSON"
        raw = b'{"schema_version":1,"nested":' + b"[" * 30000 + b"0" + b"]" * 30000 + b"}"
        if with_damaged_styles:
            styles = PersonalStyleRepository(paths.styles)
            good_style = styles.save(resolve_style(overrides={"body":{"size_pt":14}}).config,
                                     name="有效的个人样式")
            bad_style = styles.path("user." + "a" * 32)
            bad_style.write_bytes(raw)
        for repo, value in ((SettingsRepository(paths.settings), Settings(motion=False)),
                            (DraftRepository(paths.drafts), Draft.capture(
                                DocumentSession.create("fixed valid recovery"), resolve_style(), "固定恢复验证"))):
            repo.save(value)
            repo.save(value)
            repo.path.write_bytes(raw)
        window = open_window()
        wait_for(lambda:window.preview.loaded and window.session.current_text == "fixed valid recovery")
        p = window.persistence
        wait_for(lambda:p.lane.idle and p.saved_key == p.key())
        assert not window.motion.isChecked() and "恢复本地草稿" in prompts
        assert any(path.read_bytes() == raw for path in paths.drafts.glob("*.preserved-*.json"))
        report["deep_json_startup_recovers_both_backups"] = True
        if with_damaged_styles:
            assert any(i.code == "STYLE_READ_FAILED" for i in window.storage_issues)
            assert window.styles.templates.count() == 2
            assert window.styles.templates.findData("user." + "a" * 32) == -1
            window.styles.templates.setCurrentIndex(window.styles.templates.findData(good_style.id))
            assert window.styles.valid and window.styles.style.config.body.size_pt == 14
            assert bad_style.read_bytes() == raw
            report["deep_personal_style_isolated_at_startup"] = True
            report["builtin_and_valid_personal_style_usable"] = True

        report["phase"] = "real sharing lock and cancelled close"
        for text in ("older backup", "older saved draft"):
            window.editor.setPlainText(text)
            p.save_now()
            wait_for(lambda:p.lane.idle and p.saved_key == p.key())
        latest = "latest edit retained after failed clear"
        window.editor.setPlainText(latest)
        p.timer.stop()
        window.debounce.stop()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(p.repository.backup), 0x80000000, 3, None, 3, 0, None)
        assert handle != wintypes.HANDLE(-1).value
        try:
            p.clear_saved()
            wait_for(lambda:p.clear_ticket is None and p.lane.idle)
            assert p.last_issue.code == "DRAFT_CLEAR_FAILED" and p.cleared_key is None
            window.close()
            wait_for(lambda:"草稿保存失败" in prompts and not p.close_requested)
            assert not window.disposed and window.centralWidget().isEnabled()
            assert window.session.current_text == latest
        finally:
            assert kernel.CloseHandle(handle)
        report["real_lock_clear_failure_prompts_and_keeps_memory"] = True
        window.close()
        wait_for(lambda:window.disposed)
        assert DraftRepository(paths.drafts).load().value.current_text == latest
        assert any(path.read_bytes() == raw for path in paths.settings.glob("*.preserved-*.json"))
        report["close_after_unlock_saves_latest_edit"] = True

        report["phase"] = "clear then real export"
        window = open_window()
        wait_for(lambda:window.preview.loaded and window.session.current_text == latest)
        p = window.persistence
        wait_for(lambda:p.lane.idle and p.saved_key == p.key())
        p.clear_saved()
        wait_for(lambda:p.clear_ticket is None and p.lane.idle)
        assert not list(paths.drafts.glob("*.json"))
        window.start_export(OutputIntent.capture(output / "清空后导出.docx"))
        wait_for(lambda:window.last_result is not None)
        assert window.last_result.state == TaskState.SUCCEEDED and not window.session.dirty
        p.auto_save.setChecked(False)
        p.auto_save.setChecked(True)
        assert "继续编辑后自动保存" in p.status.text()
        QTest.qWait(p.SAVE_DELAY_MS + 250)
        assert not p.timer.isActive() and p.lane.idle and not list(paths.drafts.glob("*.json"))
        window.grab().save(str(output / "cleared-after-export.png"))
        window.close()
        wait_for(lambda:window.disposed)
        assert DraftRepository(paths.drafts).load().value is None
        report["clear_export_toggle_and_close_do_not_recreate_draft"] = True
        if with_damaged_styles:
            assert bad_style.read_bytes() == raw
            report["bad_style_original_bytes_preserved"] = True
        report["phase"] = "complete"
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        timer.stop()
        if window is not None and not window.disposed:
            window.persistence.close_choice = lambda:"discard"
            window.persistence.close_failure_choice = lambda:"discard"
            window.close()
            wait_for(lambda:window.disposed)
        app.processEvents()
        name = "review6-report.json" if with_damaged_styles else "review5-report.json"
        (output / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return 0 if report["passed"] else 1
