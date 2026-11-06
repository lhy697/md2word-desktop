"""Stage 05: real Pandoc integration; fault injections explicitly scoped."""
from dataclasses import FrozenInstanceError, replace
import ctypes
from ctypes import wintypes
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from md2word.domain.models import DocumentError, DocumentSession, InputMode, failure
from md2word.exporting.models import ExportSnapshot, TaskContext, TaskState, TERMINAL
from md2word.exporting.output import (OutputIntent, OutputCommitter, candidate_filename,
                                      fingerprint, locked_read)
from md2word.exporting.scheduler import Scheduler
from md2word.exporting.service import ExportService
from md2word.exporting.workspace import create_task, cleanup_task
from md2word.templates.service import resolve_style
from md2word.conversion.ooxml import Package, W, NS
from md2word.pandoc import PandocRunner, PandocError

TEXT = "# 固定快照\n\n导出时保留这一句，公式 $x^2+1$。\n\n|甲|乙|\n|---|---|\n|1|2|\n"
ROOT = Path(__file__).resolve().parents[1]


def snap(text=TEXT, mode=InputMode.MARKDOWN):
    return ExportSnapshot.capture(DocumentSession.create(text, mode), resolve_style())


def result_code(result):
    return result.issues[-1].code if result.issues else None


def run_export(tmp_path, *, snapshot=None, target=None, service=None, context=None):
    target = target or tmp_path / "成品.docx"
    service = service or ExportService(work_root=tmp_path / "tasks")
    context = context or TaskContext()
    result = service.run(snapshot or snap(), OutputIntent.capture(target), context)
    return result, context, service


def ready_context():
    c = TaskContext()
    for state in ("parsing", "checking", "writing", "postprocessing", "validating"):
        c.advance(state)
    return c


def test_snapshot_revision_and_deep_immutability():
    session = DocumentSession.create(TEXT).edit(TEXT + "\n旧版")
    style = resolve_style(overrides={"body": {"first_line_chars": 1.0}})
    captured = ExportSnapshot.capture(session.style_changed(), style)
    changed = session.edit("之后编辑的新内容").set_mode(InputMode.PLAIN).style_changed()
    assert captured.input.text.endswith("旧版")
    assert captured.input.revision == 1 and captured.style_revision == 1
    assert changed.content_revision == 3
    with pytest.raises(FrozenInstanceError):
        captured.input.text = "mutate"
    with pytest.raises(Exception):
        captured.style.config.body.first_line_chars = 3
    assert captured.reader.engine_version == "pandoc.exe 3.6.4"


def test_real_export_success_has_exact_state_order_and_cleanup(tmp_path):
    result, context, service = run_export(tmp_path)
    assert result.state == TaskState.SUCCEEDED
    assert fingerprint(result.output_path).sha256 == result.sha256
    assert result.recovery_id is None
    assert [e.state.value for e in context.drain_events()] == [
        "queued", "parsing", "checking", "writing", "postprocessing",
        "validating", "committing", "succeeded"]
    assert list(service.work_root.iterdir()) == []
    package = Package.read(result.output_path)
    assert len(package.document.xpath("//m:oMath", namespaces=NS)) == 1
    assert not context.request_cancel()
    with pytest.raises(RuntimeError):
        context.finish("failed")


def test_real_plain_export_preserves_source(tmp_path):
    text = "  空格\r\n$不是公式$\r\n\r\n|不是表格|\n"
    result, _, _ = run_export(tmp_path, snapshot=snap(text, InputMode.PLAIN))
    assert result.state == TaskState.SUCCEEDED
    doc = Package.read(result.output_path).document
    assert not doc.xpath("//m:oMath|//w:tbl", namespaces=NS)


@pytest.mark.parametrize("name,expected", [
    ("", "未命名文档.docx"), ("CON", "_CON.docx"), ("nul.txt", "_nul.txt.docx"),
    ("标题:甲/乙? ", "标题_甲_乙_.docx"), ("test.docx", "test.docx"),
    ("... ", "未命名文档.docx"), ("LPT¹", "_LPT¹.docx")])
def test_candidate_filename(name, expected):
    assert candidate_filename(name) == expected


@pytest.mark.parametrize("name", ["CON.docx", "x:stream.docx", "x?.docx", "x.docx. ", "x.txt"])
def test_invalid_target_names_rejected(tmp_path, name):
    with pytest.raises(DocumentError) as caught:
        OutputIntent.capture(tmp_path / name)
    assert caught.value.code == "OUTPUT_PATH_INVALID"


def test_no_automatic_overwrite_or_rename(tmp_path):
    path = tmp_path / "成品.docx"
    path.write_bytes(b"old")
    with pytest.raises(DocumentError) as caught:
        OutputIntent.capture(path)
    assert caught.value.code == "OUTPUT_EXISTS"
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_existing_target_changed_is_rejected_and_result_can_retry(tmp_path):
    target = tmp_path / "old.docx"
    target.write_bytes(b"old")
    intent = OutputIntent.capture(target, overwrite=True)
    target.write_bytes(b"newer bytes")
    service = ExportService(work_root=tmp_path / "tasks")
    snapshot = snap()
    context = TaskContext()
    result = service.run(snapshot, intent, context)
    assert result.state == TaskState.FAILED
    assert result_code(result) == "OUTPUT_CONFLICT"
    assert target.read_bytes() == b"newer bytes" and result.recovery_id
    # Inject parser failure to prove retry uses validated artifact, not conversion.
    with patch("md2word.exporting.service.Parser", side_effect=AssertionError("must not parse")):
        retried = service.run(snapshot, OutputIntent.capture(tmp_path / "另存.docx"),
                              TaskContext(), recovery_id=result.recovery_id)
    assert retried.state == TaskState.SUCCEEDED and retried.sha256
    assert target.read_bytes() == b"newer bytes"
    assert list(service.work_root.iterdir()) == []


def test_unchanged_authorized_overwrite_fails_closed(tmp_path):
    target = tmp_path / "old.docx"
    target.write_bytes(b"important old")
    result = ExportService(work_root=tmp_path / "tasks").run(
        snap(), OutputIntent.capture(target, overwrite=True), TaskContext())
    assert result_code(result) == "OUTPUT_OVERWRITE_UNSAFE"
    assert target.read_bytes() == b"important old" and result.recovery_id


def test_racing_new_file_is_never_clobbered_and_can_reselect(tmp_path):
    target = tmp_path / "race.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"other program won")
    scheduler = Scheduler(ExportService(work_root=tmp_path / "tasks"))
    try:
        first = scheduler.submit(snap(), intent).result(20)
        assert result_code(first) == "OUTPUT_CONFLICT" and first.recovery_id
        second = scheduler.retry(first, OutputIntent.capture(tmp_path / "retry.docx")).result(20)
        assert second.state == TaskState.SUCCEEDED
        assert target.read_bytes() == b"other program won"
    finally:
        scheduler.dispose()


def test_modified_recovery_is_never_reused(tmp_path):
    target = tmp_path / "race.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"old")
    service = ExportService(work_root=tmp_path / "tasks")
    snapshot = snap()
    first = service.run(snapshot, intent, TaskContext())
    retained = service._retained[first.recovery_id]
    retained.path.write_bytes(b"modified docx")
    second = service.run(snapshot, OutputIntent.capture(tmp_path / "retry.docx"),
                         TaskContext(), recovery_id=first.recovery_id)
    assert result_code(second) == "ARTIFACT_CHANGED"
    assert not (tmp_path / "retry.docx").exists()
    assert target.read_bytes() == b"old"


def test_recovery_expires_and_discard_is_owned(tmp_path):
    service = ExportService(work_root=tmp_path / "tasks")
    target = tmp_path / "race.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"old")
    result = service.run(snap(), intent, TaskContext())
    item = service._retained[result.recovery_id]
    service._retained[result.recovery_id] = replace(item, expires_at=0)
    retry = service.run(item.snapshot, OutputIntent.capture(tmp_path / "retry.docx"),
                        TaskContext(), recovery_id=result.recovery_id)
    assert result_code(retry) == "RECOVERY_EXPIRED"
    assert not item.task.exists()


@pytest.mark.parametrize("stage", ["parsing", "checking", "writing", "postprocessing", "validating"])
def test_cooperative_cancel_at_each_stage_is_single_terminal(tmp_path, stage):
    context = TaskContext()
    original = context.advance
    def cancelling(state):
        original(state)
        if str(state) == stage:
            context.request_cancel()
    context.advance = cancelling
    result, _, service = run_export(tmp_path, context=context)
    assert result.state == TaskState.CANCELLED
    assert not (tmp_path / "成品.docx").exists()
    assert sum(e.state in TERMINAL for e in context.drain_events()) == 1
    assert list(service.work_root.iterdir()) == []


def test_cancel_commit_boundary_is_refused_and_published_once(tmp_path, monkeypatch):
    original = os.rename
    attempts = []
    context = TaskContext()
    def rename(source, target):
        if context.state == TaskState.COMMITTING:
            attempts.append(context.request_cancel())
        return original(source, target)
    monkeypatch.setattr("md2word.exporting.output.os.rename", rename)
    result, _, _ = run_export(tmp_path, context=context)
    assert result.state == TaskState.SUCCEEDED and attempts == [False]
    assert result.output_path.exists()


def test_cancel_immediately_before_commit_is_honored(tmp_path, monkeypatch):
    original = os.fsync
    context = TaskContext()
    def fsync(fd):
        original(fd)
        context.request_cancel()
    monkeypatch.setattr("md2word.exporting.output.os.fsync", fsync)
    result, _, _ = run_export(tmp_path, context=context)
    assert result.state == TaskState.CANCELLED
    assert not (tmp_path / "成品.docx").exists()
    assert not list(tmp_path.glob(".md2word-*"))


@pytest.mark.parametrize("err,expected", [(errno.ENOSPC, "OUTPUT_DISK_FULL"), (errno.EACCES, "OUTPUT_ACCESS_DENIED")])
def test_injected_disk_full_access_error_preserves_targets(tmp_path, monkeypatch, err, expected):
    # Injection covers IO error routing, not a physical full disk.
    old = tmp_path / "old.docx"; old.write_bytes(b"old protected")
    def fail(*args):
        raise OSError(err, "injected")
    monkeypatch.setattr("md2word.exporting.output.os.fsync", fail)
    result, _, service = run_export(tmp_path)
    assert result_code(result) == expected and result.recovery_id
    assert old.read_bytes() == b"old protected"
    assert not (tmp_path / "成品.docx").exists()
    assert not list(tmp_path.glob(".md2word-*"))
    service.close()


def test_invalid_docx_injected_before_final_validation_never_commits(tmp_path):
    import md2word.exporting.service as module
    real = module.postprocess
    def corrupt(*args, **kwargs):
        result = real(*args, **kwargs)
        result.path.write_bytes(b"not zip")
        return result
    with patch.object(module, "postprocess", side_effect=corrupt):
        result, _, service = run_export(tmp_path)
    assert result.state == TaskState.FAILED and not result.recovery_id
    assert not (tmp_path / "成品.docx").exists()
    assert list(service.work_root.iterdir()) == []


def test_one_shared_deadline_across_all_real_pandoc_calls(tmp_path, monkeypatch):
    seen = []
    original = PandocRunner.run
    def observe(self, args, **kwargs):
        seen.append(kwargs["deadline"])
        return original(self, args, **kwargs)
    monkeypatch.setattr(PandocRunner, "run", observe)
    result, context, _ = run_export(tmp_path)
    assert result.state == TaskState.SUCCEEDED
    assert len(seen) >= 4 and set(seen) == {context.deadline}


def test_scheduler_busy_nonblocking_poll_snapshot_and_shutdown(tmp_path):
    entered, release = threading.Event(), threading.Event()
    service = ExportService(work_root=tmp_path / "tasks")
    original = service._build
    def paused(snapshot, context):
        entered.set()
        assert release.wait(10)
        return original(snapshot, context)
    service._build = paused
    scheduler = Scheduler(service)
    session = DocumentSession.create(TEXT)
    snapshot = ExportSnapshot.capture(session, resolve_style())
    first = scheduler.submit(snapshot, OutputIntent.capture(tmp_path / "one.docx"))
    try:
        assert entered.wait(5) and first.poll() is None
        start = time.monotonic()
        with pytest.raises(DocumentError) as caught:
            scheduler.submit(snap("new text"), OutputIntent.capture(tmp_path / "two.docx"))
        assert caught.value.code == "EXPORT_BUSY"
        assert time.monotonic() - start < .2
        changed = session.edit("entirely new editor text").style_changed()
        assert changed.current_text != first.context.task_id
        release.set()
        result = first.result(20)
        assert result.snapshot.input.text == TEXT
        assert result.state == TaskState.SUCCEEDED
        assert not (tmp_path / "two.docx").exists()
        scheduler.request_shutdown()
        assert scheduler.idle
        with pytest.raises(DocumentError):
            scheduler.submit(snapshot, OutputIntent.capture(tmp_path / "three.docx"))
    finally:
        release.set()
        first.result(20)
        scheduler.dispose()


def test_legal_state_transitions_and_cancel_queued():
    context = TaskContext()
    with pytest.raises(RuntimeError):
        context.advance("writing")
    assert context.request_cancel() and context.request_cancel()
    with pytest.raises(DocumentError):
        context.advance("parsing")
    context.finish("cancelled")
    assert [e.state.value for e in context.drain_events()] == ["queued", "cancelling", "cancelled"]


def test_deadline_is_total_not_refreshed(tmp_path):
    context = TaskContext(.01)
    time.sleep(.03)
    result, _, service = run_export(tmp_path, context=context)
    assert result_code(result) == "EXPORT_TIMEOUT"
    assert not (tmp_path / "成品.docx").exists()


def test_cleanup_rejects_wrong_owner_external_file_and_junction(tmp_path):
    root = tmp_path / "tasks"
    task, owner = create_task(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important.txt").write_bytes(b"keep")
    assert not cleanup_task(task, root, "wrong")
    junction = task / "linked"
    # Actual Windows junction, requires no developer-mode symlink permission.
    command = f'mklink /J "{junction}" "{outside}"'
    subprocess.run('cmd /d /c ' + command, check=True, capture_output=True)
    try:
        assert not cleanup_task(task, root, owner)
        assert (outside / "important.txt").read_bytes() == b"keep"
    finally:
        os.rmdir(junction)  # Remove this verified junction itself, never recursively.
    assert cleanup_task(task, root, owner)
    assert (outside / "important.txt").exists()


def test_windows_real_sharing_lock_preserves_old_file(tmp_path):
    target = tmp_path / "old.docx"; target.write_bytes(b"old protected")
    intent = OutputIntent.capture(target, overwrite=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(target), 0xC0000000, 1, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        result = ExportService(work_root=tmp_path / "tasks").run(snap(), intent, TaskContext())
        assert result_code(result) == "OUTPUT_LOCKED"
    finally:
        kernel.CloseHandle(handle)
    assert target.read_bytes() == b"old protected"


@pytest.mark.parametrize("phase", ["parsing", "checking", "writing"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_running_child_lifecycle_during_export(tmp_path, monkeypatch, phase, cancelled):
    # A real Python child is substituted for one Pandoc call to make timing
    # deterministic. The rest of the conversion uses the bundled real Pandoc.
    original = PandocRunner.run
    child_pid = tmp_path / "controlled-child.pid"
    code = ("import os,pathlib,time;"
            + "pathlib.Path(" + repr(str(child_pid)) + ").write_text(str(os.getpid()));"
            + "time.sleep(30)")
    def slow_call(self, args, **kwargs):
        matches = ((phase == "parsing" and "--to" in args and args[-1] == "json")
                   or (phase == "checking" and any(str(a).endswith("probe.docx") for a in args))
                   or (phase == "writing" and any(str(a).endswith(".building.docx") for a in args)))
        if matches:
            controlled = PandocRunner()
            controlled.executable = Path(sys.executable)
            return original(controlled, ["-c", code], **kwargs)
        return original(self, args, **kwargs)
    monkeypatch.setattr(PandocRunner, "run", slow_call)
    service = ExportService(work_root=tmp_path / "tasks")
    scheduler = Scheduler(service)
    handle = scheduler.submit(snap(), OutputIntent.capture(tmp_path / "result.docx"),
                              timeout=60 if cancelled else 2.0)
    try:
        limit = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < limit and not handle.done:
            time.sleep(.01)
        assert child_pid.exists(), handle.poll()
        assert handle.context.state.value == phase
        if cancelled:
            assert handle.cancel()
        result = handle.result(10)
        assert result.state == (TaskState.CANCELLED if cancelled else TaskState.FAILED)
        assert result_code(result) == ("EXPORT_CANCELLED" if cancelled else "EXPORT_TIMEOUT")
        assert not (tmp_path / "result.docx").exists()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        proc = kernel.OpenProcess(0x1000, False, int(child_pid.read_text()))
        if proc:
            try:
                status = wintypes.DWORD()
                assert kernel.GetExitCodeProcess(proc, ctypes.byref(status))
                assert status.value != 259
            finally:
                kernel.CloseHandle(proc)
        assert sum(e.state in TERMINAL for e in handle.context.drain_events()) == 1
        assert list(service.work_root.iterdir()) == []
    finally:
        if not handle.done:
            handle.cancel()
            handle.result(10)
        scheduler.dispose()


def test_directory_identity_change_is_conflict(tmp_path):
    parent = tmp_path / "selected"
    parent.mkdir()
    intent = OutputIntent.capture(parent / "result.docx")
    parent.rename(tmp_path / "moved")
    parent.mkdir()
    result = ExportService(work_root=tmp_path / "tasks").run(snap(), intent, TaskContext())
    assert result_code(result) == "OUTPUT_CONFLICT"
    assert not intent.path.exists()


def test_inflight_snapshot_edit_is_actual_content_frozen(tmp_path):
    entered, release = threading.Event(), threading.Event()
    session = DocumentSession.create("# 原标题\n\n原版本数字 12345")
    service = ExportService(work_root=tmp_path / "tasks")
    original = service._build
    def paused(snapshot, context):
        entered.set()
        assert release.wait(5)
        return original(snapshot, context)
    service._build = paused
    scheduler = Scheduler(service)
    handle = scheduler.submit(ExportSnapshot.capture(session, resolve_style()),
                              OutputIntent.capture(tmp_path / "frozen.docx"))
    try:
        assert entered.wait(5)
        session = session.edit("# 新标题\n\n编辑后 99999").style_changed()
        release.set()
        result = handle.result(15)
        assert result.state == TaskState.SUCCEEDED
        body = Package.read(result.output_path).document
        text = "".join(body.xpath("//w:t/text()", namespaces=NS))
        assert "12345" in text and "99999" not in text
        assert result.snapshot.input.revision == 0 and session.content_revision == 1
    finally:
        release.set()
        handle.result(15)
        scheduler.dispose()


def test_file_appears_at_atomic_publish_window_never_clobbered(tmp_path, monkeypatch):
    original = os.rename
    target = tmp_path / "成品.docx"
    context = TaskContext()
    def compete(source, destination):
        if context.state == TaskState.COMMITTING:
            target.write_bytes(b"arrived at last moment")
        return original(source, destination)
    monkeypatch.setattr("md2word.exporting.output.os.rename", compete)
    result, _, _ = run_export(tmp_path, context=context)
    assert result_code(result) == "OUTPUT_CONFLICT" and result.recovery_id
    assert target.read_bytes() == b"arrived at last moment"


def test_candidate_uses_first_real_heading_only(tmp_path):
    from md2word.exporting.output import candidate_from_document
    from md2word.parsing import Parser
    from md2word.domain.normalizer import normalize
    doc = normalize(Parser().parse(snap("普通段落\n\n## 首个 **真实标题** [链接](https://example.invalid)\n\n# 后一个").input))
    assert candidate_from_document(doc) == "首个 真实标题 链接.docx"
    plain = normalize(Parser().parse(snap("# 纯文本不是标题", InputMode.PLAIN).input))
    assert candidate_from_document(plain) == "未命名文档.docx"


def test_retained_artifact_is_automatically_expired_while_idle(tmp_path):
    service = ExportService(work_root=tmp_path / "tasks")
    service.RETAIN_SECONDS = .2
    target = tmp_path / "occupied.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"other program")
    result = service.run(snap(), intent, TaskContext())
    assert result.recovery_id
    limit = time.monotonic() + 3
    while service._retained and time.monotonic() < limit:
        time.sleep(.02)
    with service._retained_lock:
        assert not service._retained
        assert not list(service.work_root.iterdir())
    assert target.read_bytes() == b"other program"
    service.close()


def test_retry_consumes_expiry_lease_before_validation(tmp_path):
    service = ExportService(work_root=tmp_path / "tasks")
    service.RETAIN_SECONDS = .3
    target = tmp_path / "occupied.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"other program")
    first = service.run(snap(), intent, TaskContext())
    real_commit = service.committer.commit
    observed = []
    def paused(artifact, *args):
        time.sleep(.5)  # Beyond the old artifact lease, but within this task's budget.
        observed.append(artifact.exists())
        return real_commit(artifact, *args)
    service.committer.commit = paused
    second = service.run(first.snapshot, OutputIntent.capture(tmp_path / "retry.docx"),
                         TaskContext(), recovery_id=first.recovery_id)
    assert observed == [True] and second.state == TaskState.SUCCEEDED
    assert not service._retained
    service.close()


def test_confirmed_target_deleted_is_conflict_and_can_retry(tmp_path):
    target = tmp_path / "confirmed.docx"
    target.write_bytes(b"confirmed version")
    intent = OutputIntent.capture(target, overwrite=True)
    target.unlink()  # An external change after the explicit confirmation.
    service = ExportService(work_root=tmp_path / "tasks")
    snapshot = snap()
    try:
        result = service.run(snapshot, intent, TaskContext())
        assert result.state == TaskState.FAILED
        assert result_code(result) == "OUTPUT_CONFLICT" and result.recovery_id
        assert not target.exists()  # An overwrite intent cannot become new-file permission.
        retried = service.run(snapshot, OutputIntent.capture(tmp_path / "new.docx"),
                              TaskContext(), recovery_id=result.recovery_id)
        assert retried.state == TaskState.SUCCEEDED
        assert not target.exists()
        assert not list(service.work_root.iterdir())
    finally:
        service.close()


def test_deleted_retained_docx_is_artifact_changed_not_output_error(tmp_path):
    target = tmp_path / "racing.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"other program content")
    service = ExportService(work_root=tmp_path / "tasks")
    snapshot = snap()
    try:
        first = service.run(snapshot, intent, TaskContext())
        assert first.recovery_id
        service._retained[first.recovery_id].path.unlink()
        retry = service.run(snapshot, OutputIntent.capture(tmp_path / "retry.docx"),
                            TaskContext(), recovery_id=first.recovery_id)
        assert retry.state == TaskState.FAILED
        assert result_code(retry) == "ARTIFACT_CHANGED"
        assert retry.recovery_id is None and not (tmp_path / "retry.docx").exists()
        assert target.read_bytes() == b"other program content"
        assert not list(service.work_root.iterdir())
    finally:
        service.close()





