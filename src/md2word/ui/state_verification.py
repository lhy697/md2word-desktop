"""Opt-in, isolated fixed-data verification; never accesses production user state."""
import json
from pathlib import Path
import zipfile

from PySide6.QtCore import QTimer, QEventLoop
from PySide6.QtWidgets import QApplication, QMessageBox

from ..domain.models import DocumentSession, InputMode
from ..exporting.models import TaskState
from ..exporting.output import OutputIntent
from ..exporting.scheduler import Scheduler
from ..exporting.service import ExportService
from ..storage.files import UserPaths
from ..storage.instance import SingleInstance
from ..storage.repositories import DraftRepository
from ..templates.service import PersonalStyleRepository
from .main_window import MainWindow
from .verification import wait_for, javascript


MARKER = "STAGE7_PRIVATE_FIXED_SENTINEL_56789"
ORIGINAL = "原始输入\r\n" + MARKER
BACKUP_TEXT = "上一次有效草稿 A：12345 😀"
CURRENT_TEXT = "最近有效草稿 B：67890 😀"


def verify_state_worker(output_directory, worker):
    if worker in ("review5", "review6"):
        from .recovery_verification import verify_review5
        return verify_review5(output_directory, with_damaged_styles=worker == "review6")
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("LocalTools")
    app.setApplicationName("md2word-stage7-qa")
    paths = UserPaths(output / "isolated-user-data")
    guard = SingleInstance(paths)
    window = None
    report = {"passed":False, "worker":worker, "fixed_sample":True, "production_state_used":False}
    try:
        primary = guard.acquire()
        if worker == "activate":
            report["passed"] = primary is False
            report["second_writer_created"] = primary
            return 0 if report["passed"] else 1
        if not primary:
            raise RuntimeError("Expected primary")
        report["primary_lock_acquired"] = True
        timer = QTimer()
        timer.setInterval(40)
        prompt_seen = []
        def recover_dialog():
            modal = QApplication.activeModalWidget()
            if isinstance(modal, QMessageBox) and modal.windowTitle() == "恢复本地草稿":
                prompt_seen.append(modal.text())
                modal.grab().save(str(output / "recovery-dialog.png"))
                next(b for b in modal.buttons() if b.text() == "恢复草稿").click()
        timer.timeout.connect(recover_dialog)
        if worker == "recover":
            timer.start()
        window = MainWindow(user_paths=paths, style_repository=PersonalStyleRepository(paths.styles),
                            scheduler=Scheduler(ExportService(work_root=paths.tasks)))
        window.may_replace = lambda:True
        def activate():
            window.showNormal()
            window.raise_()
            window.activateWindow()
            (output / "activation-received.json").write_text('{"activated":true}', "utf-8")
        guard.activated.connect(activate)
        window.show()
        wait_for(lambda:window.preview.loaded)
        p = window.persistence
        if worker == "seed":
            window.replace_session(DocumentSession.create(ORIGINAL, InputMode.PLAIN).edit(BACKUP_TEXT), "固定恢复验证")
            window.styles.fields["body.size_pt"].setEditText("13.5")
            p.save_now()
            wait_for(lambda:p.lane.idle and p.saved_key == p.key())
            window.editor.setPlainText(CURRENT_TEXT)
            p.save_now()
            wait_for(lambda:p.lane.idle and p.saved_key == p.key())
            assert DraftRepository(paths.drafts).load().value.current_text == CURRENT_TEXT
            assert p.repository.backup.is_file()
            window.grab().save(str(output / "autosaved-desktop.png"))
            (output / "seed-ready.json").write_text(json.dumps({"passed":True, "backup_saved":True}), "utf-8")
            # The harness deliberately kills this process after a real second launch.
            return app.exec()
        wait_for(lambda:window.session.current_text == BACKUP_TEXT)
        timer.stop()
        window.motion.setChecked(False)
        assert prompt_seen and "上一次有效快照" in prompt_seen[0]
        assert window.session.original_text == ORIGINAL
        assert window.session.input_mode == InputMode.PLAIN
        assert window.styles.style.config.body.size_pt == 13.5
        report.update(backup_recovered=True, original_preserved=True, mode_restored=True, style_restored=True,
                      recovery_prompt_shown=True)
        window.request_preview()
        wait_for(lambda:window.latest_preview is not None)
        wait_for(lambda:bool(javascript(window, "window.previewState.ready")))
        assert BACKUP_TEXT in javascript(window, "document.getElementById('paper').textContent")
        report["restored_content_visible_in_web_preview"] = True
        target = output / "恢复后的讲义.docx"
        window.start_export(OutputIntent.capture(target))
        wait_for(lambda:window.last_result is not None)
        assert window.last_result.state == TaskState.SUCCEEDED
        report["real_export_succeeded"] = True
        p.logs.bundle(output / "default-diagnostics.zip")
        with zipfile.ZipFile(output / "default-diagnostics.zip") as archive:
            assert set(archive.namelist()) == {"summary.json", "events.jsonl"}
            assert all(MARKER.encode() not in archive.read(name) and BACKUP_TEXT.encode() not in archive.read(name)
                       for name in archive.namelist())
        report["default_diagnostic_excludes_content"] = True
        window.resize(720, 680)
        settle = QEventLoop()
        QTimer.singleShot(350, settle.quit)
        settle.exec()
        window.grab().save(str(output / "small-recovered.png"))
        assert window.compact and window.width() <= 740
        report["small_window_usable"] = True
        window.resize(1440, 960)
        settle = QEventLoop()
        QTimer.singleShot(350, settle.quit)
        settle.exec()
        window.grab().save(str(output / "recovered-desktop.png"))
        window.close()
        wait_for(lambda:window.disposed)
        report["latest_draft_saved_on_close"] = DraftRepository(paths.drafts).load().value.current_text == BACKUP_TEXT
        report["temporary_export_tasks_cleaned"] = not list(paths.tasks.glob("export-*"))
        report["passed"] = report["latest_draft_saved_on_close"] and report["temporary_export_tasks_cleaned"]
        return 0 if report["passed"] else 1
    except Exception as exc:
        report["error"] = type(exc).__name__
        return 1
    finally:
        if window is not None and not window.disposed:
            window.persistence.close_choice = lambda:"discard"
            window.persistence.close_failure_choice = lambda:"discard"
            window.close()
            wait_for(lambda:window.disposed)
        guard.close()
        app.processEvents()
        (output / (worker + "-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
