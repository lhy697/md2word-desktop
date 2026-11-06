"""Stage 07 fault/recovery contracts, with real files, Qt, child processes and DOCX."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication
from md2word.domain.models import DocumentSession, InputMode, DocumentError, failure
from md2word.exporting.output import OutputIntent
from md2word.exporting.scheduler import Scheduler
from md2word.exporting.service import ExportService
from md2word.exporting.models import TaskState
from md2word.storage.files import UserPaths, json_bytes
from md2word.storage.models import Draft, Settings
from md2word.storage.repositories import DraftRepository, SettingsRepository
from md2word.storage.lane import DraftLane
from md2word.storage.diagnostics import Diagnostics
from md2word.storage.instance import SingleInstance
from md2word.templates.service import resolve_style, PersonalStyleRepository
from md2word.ui.main_window import MainWindow
from md2word.ui.persistence import PersistenceController
from test_stage6 import app, spin


MARKER = "PRIVATE_STAGE7_原文标记_123456"
ROOT = Path(__file__).resolve().parents[1]


def draft(text="当前内容", revision=1):
    session = DocumentSession.create(MARKER + "\r\n原文😀", InputMode.PLAIN).edit(text)
    session = replace(session, content_revision=revision, style_revision=3)
    return Draft.capture(session, resolve_style(overrides={"body":{"size_pt":13.5}}), "固定草稿测试")


@pytest.fixture
def paths(tmp_path):
    return UserPaths(tmp_path / "中文 用户数据")


@pytest.fixture
def state_window(app, paths, monkeypatch):
    monkeypatch.setattr(PersistenceController, "offer_recovery", lambda self,*_:None)
    monkeypatch.setattr(PersistenceController, "SAVE_DELAY_MS", 100)
    w = MainWindow(user_paths=paths, style_repository=PersonalStyleRepository(paths.styles),
                   scheduler=Scheduler(ExportService(work_root=paths.tasks)))
    w.may_replace = lambda:True
    w.show()
    spin(lambda:w.preview.loaded)
    yield w
    if not w.disposed:
        w.persistence.close_choice = lambda:"discard"
        w.persistence.close_failure_choice = lambda:"discard"
        w.close()
        spin(lambda:w.disposed)
    app.processEvents()


def test_self_contained_draft_survives_deleted_personal_style(paths):
    styles = PersonalStyleRepository(paths.styles)
    style = styles.save(resolve_style().config, name="已删除的个人样式")
    session = DocumentSession.create("原始\r\n" + MARKER, InputMode.PLAIN).edit("当前\r\n😀正文")
    repo = DraftRepository(paths.drafts)
    saved = Draft.capture(session, resolve_style(personal=style), "测试")
    repo.save(saved)
    styles.delete(style.id)
    loaded = DraftRepository(paths.drafts).load().value
    assert loaded.session() == session and loaded.style().config == style
    assert loaded.original_text == "原始\r\n" + MARKER
    assert loaded.current_text == "当前\r\n😀正文"


def test_atomic_replace_failure_preserves_prior_snapshot_and_cleans_owned_temp(paths, monkeypatch):
    from md2word.storage import files
    repo = DraftRepository(paths.drafts)
    first, second = draft("old"), draft("new", 2)
    repo.save(first)
    original = files.os.replace
    def deny_main(source, target):
        if Path(target) == repo.path:
            raise PermissionError("explicit write-failure injection " + MARKER)
        return original(source, target)
    monkeypatch.setattr(files.os, "replace", deny_main)
    with pytest.raises(DocumentError, match="保存失败") as error:
        repo.save(second)
    assert MARKER not in str(error.value)
    assert repo.load().value == first and repo.backup.exists()
    assert not list(paths.drafts.glob("*.tmp"))


def test_corrupt_primary_recovers_backup_and_preserves_damaged_bytes(paths):
    repo = DraftRepository(paths.drafts)
    first, second = draft("backup"), draft("primary", 2)
    repo.save(first)
    repo.save(second)
    damaged = b'{"schema_version":1,broken'
    repo.path.write_bytes(damaged)
    loaded = repo.load()
    assert loaded.value == first and loaded.source == "上一次有效快照" and loaded.issues
    assert repo.path.read_bytes() == damaged
    repo.save(first)
    assert repo.load().value == first
    assert next(paths.drafts.glob("session.preserved-*.json")).read_bytes() == damaged


def test_both_drafts_corrupt_are_protected_until_explicit_clear(paths):
    repo = DraftRepository(paths.drafts)
    repo.path.write_bytes(b"damaged primary")
    repo.backup.write_bytes(b"damaged backup")
    loaded = repo.load()
    assert loaded.value is None and repo.blocked and len(loaded.issues) == 2
    with pytest.raises(DocumentError):
        repo.save(draft())
    assert repo.path.read_bytes() == b"damaged primary"
    repo.clear()
    assert not repo.path.exists() and not repo.backup.exists()
    repo.save(draft())
    assert repo.load().value is not None


@pytest.mark.parametrize("which", ["draft-primary", "draft-backup", "settings"])
def test_future_schemas_never_get_rewritten(paths, which):
    repo = SettingsRepository(paths.settings) if which == "settings" else DraftRepository(paths.drafts)
    value = Settings() if which == "settings" else draft()
    repo.save(value)
    future = b'{"schema_version":99,"unrecognized":"keep me"}'
    path = repo.backup if which == "draft-backup" else repo.path
    path.write_bytes(future)
    loaded = repo.load()
    assert loaded.value is None and repo.blocked
    assert any(i.code.endswith("INCOMPATIBLE") for i in loaded.issues)
    with pytest.raises(DocumentError):
        repo.save(value)
    assert path.read_bytes() == future


@pytest.mark.parametrize("kind", ["draft", "settings"])
def test_known_v0_migration_preserves_exact_old_file_before_new_commit(paths, kind):
    if kind == "draft":
        repo = DraftRepository(paths.drafts)
        raw = draft().model_dump(mode="json")
        raw.update(schema_version=0, revision=raw.pop("content_revision"))
        raw.pop("style_revision")
        raw.pop("dirty")
        raw.pop("local_formats")
    else:
        repo = SettingsRepository(paths.settings)
        raw = dict(schema_version=0, geometry=[12,34,1000,800], autosave=False, last_directory="", motion=False)
    before = json_bytes(raw)
    repo.path.write_bytes(before)
    loaded = repo.load()
    assert loaded.value.schema_version == (2 if kind == "draft" else 1) and repo.path.read_bytes() == before
    repo.save(loaded.value)
    assert (repo.directory / (repo.path.stem + ".v0.json")).read_bytes() == before
    assert json.loads(repo.path.read_text("utf-8"))["schema_version"] == (2 if kind == "draft" else 1)


def test_latest_only_writer_prevents_old_late_save_from_overwriting_new(paths):
    repo = DraftRepository(paths.drafts)
    original = repo.save
    entered, release = threading.Event(), threading.Event()
    writes = []
    def slow_save(value):
        if not writes:
            entered.set()
            assert release.wait(5)
        writes.append(value.content_revision)
        return original(value)
    repo.save = slow_save
    lane = DraftLane(repo)
    try:
        lane.request(draft("old",1))
        assert entered.wait(2)
        for rev in range(2, 52):
            lane.request(draft("new",rev))
        assert lane.pending[0] == 51
        release.set()
        results = []
        deadline = time.monotonic() + 5
        while not lane.idle and time.monotonic() < deadline:
            item = lane.poll()
            if item:
                results.append(item)
            time.sleep(.01)
        assert lane.idle and writes == [1,51] and lane.last_saved == 51
        assert repo.load().value.content_revision == 51
        assert all(r.issue is None for r in results)
    finally:
        release.set()
        if lane.idle:
            lane.close()


def test_disabling_writer_drops_waiting_save_but_preserves_completed_snapshot(paths):
    repo = DraftRepository(paths.drafts)
    original = repo.save
    release, entered = threading.Event(), threading.Event()
    def blocked_save(value):
        entered.set()
        assert release.wait(5)
        return original(value)
    repo.save = blocked_save
    lane = DraftLane(repo)
    lane.request(draft("in flight",1))
    assert entered.wait(2)
    lane.request(draft("must not write",2))
    lane.pause()
    release.set()
    deadline = time.monotonic() + 5
    while not lane.idle and time.monotonic() < deadline:
        lane.poll()
        time.sleep(.01)
    assert lane.idle and repo.load().value.current_text == "in flight"
    lane.close()


def test_real_crash_leaves_recoverable_draft_and_stale_lock_is_reclaimed(app, paths):
    script = '''
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from md2word.storage.files import UserPaths
from md2word.storage.instance import SingleInstance
from md2word.storage.repositories import DraftRepository
from md2word.storage.models import Draft
from md2word.domain.models import DocumentSession
from md2word.templates.service import resolve_style
app=QCoreApplication([])
paths=UserPaths(Path(sys.argv[1]))
guard=SingleInstance(paths)
assert guard.acquire()
DraftRepository(paths.drafts).save(Draft.capture(DocumentSession.create("crash saved 12345"),resolve_style(),"fixed"))
(paths.root/"ready").write_text("ready")
app.exec()
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen([sys.executable,"-c",script,str(paths.root)], env=env,
                            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:
        spin(lambda:(paths.root / "ready").exists(), timeout=10000)
        proc.kill()
        proc.wait(timeout=5)
        assert (paths.root / "application.lock").exists()
        guard = SingleInstance(paths)
        try:
            assert guard.acquire()
            assert DraftRepository(paths.drafts).load().value.current_text == "crash saved 12345"
        finally:
            guard.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_second_instance_only_activates_existing_owner(app, paths):
    first, second = SingleInstance(paths), SingleInstance(paths)
    activated = []
    first.activated.connect(lambda:activated.append(True))
    try:
        assert first.acquire()
        assert second.acquire() is False
        spin(lambda:bool(activated))
        assert first.primary and not second.primary
    finally:
        second.close()
        first.close()


def test_default_logs_and_diagnostic_bundle_exclude_private_content(paths, tmp_path):
    logs = Diagnostics(paths.logs)
    logs.record("startup", "APPLICATION_STARTED")
    logs.record("storage", "DRAFT_SAVE_FAILED", 200)
    with pytest.raises(ValueError):
        logs.record("storage", MARKER)
    archive = tmp_path / "safe.zip"
    logs.bundle(archive)
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"summary.json", "events.jsonl"}
        assert all(MARKER.encode() not in bundle.read(n) for n in bundle.namelist())
        assert str(paths.root).encode() not in bundle.read("events.jsonl")
    selected = tmp_path / "selected.zip"
    logs.bundle(selected, session=DocumentSession.create(MARKER))
    with zipfile.ZipFile(selected) as bundle:
        assert MARKER.encode() in bundle.read("content/original.txt")
    with pytest.raises(DocumentError):
        logs.bundle(archive)
    logs.close()


def test_log_rotation_is_bounded_and_does_not_accept_raw_log_text(paths):
    logs = Diagnostics(paths.logs)
    logs.MAX_BYTES = 1024
    logs.handler.maxBytes = 1024
    for _ in range(90):
        logs.record("storage", "DRAFT_SAVE_FAILED")
    logs.close()
    files = list(paths.logs.iterdir())
    assert len(files) <= 4 and sum(p.stat().st_size for p in files) < 6000


def test_ui_autosaves_content_mode_and_style_then_restores_exact_session(state_window):
    w, p = state_window, state_window.persistence
    session = DocumentSession.create("original\r\n" + MARKER).edit("current😀")
    w.replace_session(session, "恢复测试")
    w.mode.setCurrentIndex(1)
    w.styles.fields["body.size_pt"].setEditText("13.5")
    expected = w.session
    spin(lambda:p.lane.idle and p.repository.path.exists() and p.saved_key == p.key())
    stored = p.repository.load().value
    assert stored.session() == expected and stored.style().config.body.size_pt == 13.5
    w.replace_session(DocumentSession.create("replacement"))
    p.recovery = p.repository.load()
    p.recovery_pending = True
    p.restore_draft()
    assert w.session == expected and w.editor.toPlainText() == expected.current_text
    assert w.styles.style == stored.style() and not p.recovery_pending


def test_ui_disable_autosave_retains_draft_and_clear_prevents_recreation_on_close(state_window):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("saved snapshot")
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    p.auto_save.setChecked(False)
    before = p.repository.path.read_bytes()
    w.editor.setPlainText("unsaved changes")
    QApplication.processEvents()
    assert not p.timer.isActive() and p.repository.path.read_bytes() == before
    p.clear_saved()
    spin(lambda:p.clear_ticket is None)
    assert not p.repository.path.exists() and not p.repository.backup.exists()
    assert w.editor.toPlainText() == "unsaved changes"
    w.close()
    spin(lambda:w.disposed)
    assert not (p.paths.drafts / "session.json").exists()


def test_ui_close_waits_for_latest_draft_and_freezes_edits(state_window):
    w, p = state_window, state_window.persistence
    release, entered = threading.Event(), threading.Event()
    original = p.repository.save
    def slow(value):
        entered.set()
        assert release.wait(5)
        return original(value)
    p.repository.save = slow
    w.editor.setPlainText("first")
    p.save_now()
    assert entered.wait(2)
    w.editor.setPlainText("latest before close")
    w.close()
    assert not w.disposed and not w.centralWidget().isEnabled()
    release.set()
    spin(lambda:w.disposed)
    assert DraftRepository(p.paths.drafts).load().value.current_text == "latest before close"


def test_ui_draft_failure_cancels_close_and_keeps_export_service_usable(state_window, tmp_path):
    w, p = state_window, state_window.persistence
    original = p.repository.save
    p.repository.save = lambda _: (_ for _ in ()).throw(failure("DRAFT_SAVE_FAILED","明确注入写入失败","重试","storage"))
    p.close_failure_choice = lambda:"cancel"
    w.editor.setPlainText("retain " + MARKER)
    w.close()
    spin(lambda:not p.close_requested)
    assert not w.disposed and w.centralWidget().isEnabled() and MARKER in w.editor.toPlainText()
    assert any(i.code == "DRAFT_SAVE_FAILED" for i in w.storage_issues)
    p.repository.save = original
    w.start_export(OutputIntent.capture(tmp_path / "after-cancel.docx"))
    spin(lambda:w.last_result is not None, timeout=30000)
    assert w.last_result.state == TaskState.SUCCEEDED


def test_ui_settings_fall_back_to_visible_screen_and_ignore_missing_output_directory(app, paths, monkeypatch):
    monkeypatch.setattr(PersistenceController,"offer_recovery",lambda self,*_:None)
    SettingsRepository(paths.settings).save(Settings(x=90000,y=90000,width=15000,height=15000,
                                                     last_directory=str(paths.root / "does not exist"), motion=False))
    w = MainWindow(user_paths=paths,style_repository=PersonalStyleRepository(paths.styles),
                   scheduler=Scheduler(ExportService(work_root=paths.tasks)))
    w.show()
    try:
        spin(lambda:not w.motion.isChecked())
        available = QApplication.primaryScreen().availableGeometry()
        assert available.contains(w.geometry().center()) and w.width() <= available.width()
        assert w.last_directory is None
    finally:
        w.close()
        spin(lambda:w.disposed)


def test_clear_waits_for_in_flight_write_and_does_not_resurrect_backup(state_window):
    w, p = state_window, state_window.persistence
    release, entered = threading.Event(), threading.Event()
    original = p.repository.save
    def slow(value):
        entered.set()
        assert release.wait(5)
        return original(value)
    p.repository.save = slow
    w.editor.setPlainText("save then explicitly clear")
    p.save_now()
    assert entered.wait(2)
    p.clear_saved()
    release.set()
    spin(lambda:p.clear_ticket is None and p.lane.idle)
    assert p.repository.load().value is None and not p.repository.backup.exists()
    w.close()
    spin(lambda:w.disposed)
    assert DraftRepository(p.paths.drafts).load().value is None


def test_ui_close_during_export_waits_without_damaging_draft_or_leaving_tasks(state_window, tmp_path):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("# 关闭协调\n\n" + MARKER)
    w.start_export(OutputIntent.capture(tmp_path / "closing.docx"))
    w.close()
    spin(lambda:w.disposed, timeout=30000)
    stored = DraftRepository(p.paths.drafts).load().value
    assert MARKER in stored.current_text
    assert w.last_result.state in (TaskState.CANCELLED, TaskState.SUCCEEDED)
    assert not list(p.paths.tasks.glob("export-*"))


def test_many_save_restore_export_cycles_keep_threads_and_workspaces_bounded(state_window, tmp_path):
    w, p = state_window, state_window.persistence
    for index in range(4):
        w.editor.setPlainText(f"# 循环 {index}\n\n固定正文 12345")
        p.save_now()
        spin(lambda:p.lane.idle and p.saved_key == p.key())
        stored = p.repository.load().value
        w.editor.insertPlainText("temporary edit")
        p.recovery = p.repository.load()
        p.recovery_pending = True
        p.restore_draft()
        assert w.session == stored.session()
        previous = w.last_result
        w.start_export(OutputIntent.capture(tmp_path / f"cycle-{index}.docx"))
        spin(lambda:w.last_result is not previous, timeout=30000)
        assert w.last_result.state == TaskState.SUCCEEDED
        assert not list(p.paths.tasks.glob("export-*"))
    assert len([t for t in threading.enumerate() if t.name.startswith("md2word-draft")]) <= 1
    assert len(list(p.paths.drafts.glob("*.json"))) <= 2
    assert not list(p.paths.drafts.glob("*.tmp"))


def test_future_settings_stay_intact_while_drafts_can_still_be_saved(app, paths, monkeypatch):
    monkeypatch.setattr(PersistenceController,"offer_recovery",lambda self,*_:None)
    repo = SettingsRepository(paths.settings)
    protected = b'{"schema_version":123,"retain":"future setting"}'
    repo.path.write_bytes(protected)
    w = MainWindow(user_paths=paths, style_repository=PersonalStyleRepository(paths.styles),
                   scheduler=Scheduler(ExportService(work_root=paths.tasks)))
    w.show()
    try:
        spin(lambda:w.preview.loaded)
        w.editor.setPlainText("future settings do not destroy content")
        w.persistence.save_now()
        spin(lambda:w.persistence.lane.idle and w.persistence.saved_key == w.persistence.key())
        w.persistence.auto_save.setChecked(False)
        assert repo.path.read_bytes() == protected
    finally:
        w.persistence.close_choice = lambda:"save"
        w.close()
        spin(lambda:w.disposed)
    assert repo.path.read_bytes() == protected


def test_corrupt_personal_style_does_not_block_builtin_or_other_styles(paths):
    styles = PersonalStyleRepository(paths.styles)
    valid = styles.save(resolve_style().config, name="有效样式")
    bad_id = "user." + "a" * 32
    styles.path(bad_id).write_text("{bad", "utf-8")
    listing = styles.list()
    assert {s.id for s in listing.styles} == {"lecture",valid.id}
    assert listing.issues and styles.load(valid.id) == valid
    assert styles.path(bad_id).read_text("utf-8") == "{bad"


def test_empty_current_text_still_keeps_original_input_in_draft(state_window):
    w, p = state_window, state_window.persistence
    w.replace_session(DocumentSession.create(MARKER))
    w.editor.clear()
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    loaded = p.repository.load().value
    assert loaded.current_text == "" and loaded.original_text == MARKER


def test_explicit_clear_removes_migration_and_preserved_copies(paths):
    repo = DraftRepository(paths.drafts)
    repo.save(draft())
    (paths.drafts / "session.v0.json").write_bytes(b"old known version")
    (paths.drafts / "session.preserved-damaged-abc.json").write_bytes(b"damaged")
    repo.clear()
    assert not list(paths.drafts.glob("*.json"))


def test_disabling_autosave_during_pending_clear_still_completes_clear(state_window):
    w, p = state_window, state_window.persistence
    original = p.repository.save
    release, entered = threading.Event(), threading.Event()
    def slow(value):
        entered.set()
        assert release.wait(5)
        return original(value)
    p.repository.save = slow
    w.editor.setPlainText("in flight before clear")
    p.save_now()
    assert entered.wait(2)
    p.clear_saved()
    p.auto_save.setChecked(False)
    release.set()
    spin(lambda:p.clear_ticket is None and p.lane.idle)
    assert p.repository.load().value is None
    w.close()
    spin(lambda:w.disposed)


def test_restore_with_autosave_disabled_does_not_rewrite_existing_file(state_window):
    w, p = state_window, state_window.persistence
    w.editor.setPlainText("saved original")
    p.save_now()
    spin(lambda:p.lane.idle and p.saved_key == p.key())
    p.auto_save.setChecked(False)
    before = p.repository.path.read_bytes()
    w.editor.setPlainText("unsaved replacement")
    p.recovery = p.repository.load()
    p.recovery_pending = True
    p.restore_draft()
    assert w.editor.toPlainText() == "saved original"
    assert p.lane.idle and not p.timer.isActive() and p.repository.path.read_bytes() == before


def test_opening_and_closing_untouched_window_does_not_create_empty_recovery_prompt(state_window):
    w, p = state_window, state_window.persistence
    w.close()
    spin(lambda:w.disposed)
    assert DraftRepository(p.paths.drafts).load().value is None
