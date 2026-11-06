"""Regression cases from artifacts/review-20261101/审查报告.md.

All ordinary conversions use bundled Pandoc. Cleanup locks are real Windows
handles; only private, generated test documents are touched.
"""
from dataclasses import replace
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import zipfile

import pytest
from lxml import etree as ET

from md2word.conversion.math_check import MathChecker
from md2word.conversion.ooxml import Package, NS, W, q, plain_word_text
from md2word.conversion.postprocess import postprocess
from md2word.conversion.service import ConversionService
from md2word.conversion.validation import Reader, validate_docx
from md2word.conversion.write_plan import all_paragraphs, build_write_plan
from md2word.domain.models import DocumentSession, Severity, failure
from md2word.domain.normalizer import normalize
from md2word.domain.style_models import mm_to_twips
from md2word.exporting.models import ExportSnapshot, TaskContext, TaskState
from md2word.exporting.output import OutputIntent, locked_read
from md2word.exporting.service import ExportService
from md2word.exporting.workspace import cleanup_task, create_task
from md2word.parsing import Parser
from md2word.templates.service import resolve_style


def snapshot(text):
    return ExportSnapshot.capture(DocumentSession.create(text), resolve_style())


def blocked_by_competitor(service, tmp_path, name="target"):
    target = tmp_path / (name + ".docx")
    intent = OutputIntent.capture(target)
    target.write_bytes(b"other writer owns this file")
    result = service.run(snapshot("# public sample\n\n12345"), intent, TaskContext())
    assert result.recovery_id
    return result, service._retained[result.recovery_id]


@pytest.mark.parametrize("source,continuations", [
    ("1. first\n\n   continuation\n\n2. next\n", {"continuation": 0}),
    ("- first\n\n  continuation\n\n- next\n", {"continuation": 0}),
    ("1. outer\n\n   outer continuation\n\n   - inner\n\n     inner continuation\n\n2. next\n",
     {"outer continuation": 0, "inner continuation": 1}),
    ("> 1. first\n>\n>    continuation\n>\n> 2. next\n", {"continuation": 0}),
], ids=["ordered", "bullet", "nested", "quoted"])
def test_list_continuation_ignores_body_first_indent(tmp_path, source, continuations):
    text = source + "\nA normal body paragraph.\n"
    snap = DocumentSession.create(text).snapshot()
    service = ConversionService()
    seen = []
    for first_chars in (1, 3):
        style = resolve_style(overrides={"body": {"first_line_chars": first_chars}})
        result = service.convert_temp(snap, work_root=tmp_path, style=style)
        assert result.validation.passed
        package = Package.read(result.docx_path)
        records = {}
        for paragraph in package.document.findall(".//w:body/w:p", NS):
            visible = plain_word_text(paragraph)
            prop = paragraph.find("w:pPr/w:ind", NS)
            if visible in continuations:
                assert prop is not None
                assert prop.get(q(W, "left")) == str(mm_to_twips(
                    (continuations[visible] + 1) * style.config.lists.indent_mm))
                assert prop.get(q(W, "firstLineChars"), "0") == "0"
                assert prop.get(q(W, "firstLine"), "0") == "0"
                assert prop.get(q(W, "hanging"), "0") == "0"
                records[visible] = dict(prop.attrib)
            if visible == "A normal body paragraph.":
                assert prop.get(q(W, "firstLineChars")) == str(first_chars * 100)
        assert records.keys() == continuations.keys()
        seen.append(records)
    assert seen[0] == seen[1]


@pytest.mark.parametrize("source,expected_numbers", [
    ("1. $$x^2$$\n2. $$y^2$$\n", [1, 2]),
    ("3. $$x^2$$\n4. text\n5. $$y^2$$\n", [3, 4, 5]),
    ("- $$x^2$$\n- $$y^2$$\n", [1, 2]),
    ("1. outer\n   1. $$x^2$$\n   2. $$y^2$$\n2. next\n", [1, 1, 2, 2]),
], ids=["ordered", "mixed-restart", "bullet", "nested"])
def test_math_only_list_has_real_validated_numbering(tmp_path, source, expected_numbers):
    snap = snapshot(source)
    service = ExportService(work_root=tmp_path / "tasks")
    target = tmp_path / "out.docx"
    try:
        result = service.run(snap, OutputIntent.capture(target), TaskContext())
        assert result.state == TaskState.SUCCEEDED, [i.code for i in result.issues]
        package = Package.read(target)
        reader = Reader(package)
        rules = [reader.numbering(p) for p in package.document.findall(".//w:body/w:p", NS)]
        assert all(rule is not None for rule in rules)
        assert [rule[-1] for rule in rules] == expected_numbers
        assert len(package.document.findall(".//m:oMath", NS)) == 2
    finally:
        service.close()


@pytest.mark.parametrize("text,summary", [
    ("   ", "DOCUMENT_BLOCKED"),
    ("before\n\n$$\n$$\n", "MATH_UNCERTAIN"),
    ("![desc](test.png)\n\n$$\n$$\n", "MATH_UNCERTAIN"),
])
def test_blocked_export_keeps_all_original_issues_and_source(tmp_path, text, summary):
    snap = snapshot(text)
    parser = Parser()
    document = normalize(parser.parse(snap.input, cwd=tmp_path))
    expected = document.issues
    if document.exportable:
        checked = MathChecker(parser.runner).check_math(
            document, work_dir=tmp_path, reference_key="lecture")
        expected = checked.issues
    service = ExportService(work_root=tmp_path / "tasks")
    try:
        result = service.run(snap, OutputIntent.capture(tmp_path / "out.docx"), TaskContext())
        assert result.state == TaskState.FAILED
        assert all(issue in result.issues for issue in expected)
        assert result.failure.code == summary
        assert not (tmp_path / "out.docx").exists()
        blocking = [i for i in result.issues if i.severity == Severity.BLOCKING]
        assert blocking and len(blocking) == len([i for i in expected if i.severity == Severity.BLOCKING])
        for issue in result.issues:
            if issue.node_id:
                assert issue.location and issue.location.session_id == snap.input.session_id
                assert issue.location.revision == snap.input.revision
    finally:
        service.close()


def test_writer_failure_keeps_prior_warning(tmp_path, monkeypatch):
    def fail_writer(*args, **kwargs):
        raise failure("WRITER_TEST_FAILURE", "fixed test failure", "retry", "export")
    monkeypatch.setattr("md2word.exporting.service.DocxWriter.write_docx", fail_writer)
    service = ExportService(work_root=tmp_path / "tasks")
    try:
        result = service.run(snapshot("![desc](test.png)"), OutputIntent.capture(tmp_path / "out.docx"), TaskContext())
        assert [i.code for i in result.issues] == ["IMAGE_NOT_SUPPORTED", "WRITER_TEST_FAILURE"]
        assert result.issues[0].location is not None
        assert result.failure.code == "WRITER_TEST_FAILURE"
    finally:
        service.close()


class LockedCancellation:
    guard = None

    def commit(self, artifact, expected_sha, intent, context):
        self.guard = locked_read(artifact)
        self.guard.__enter__()
        context.request_cancel()
        context.check()


def test_cancel_cleanup_keeps_marker_diagnostic_and_retry(tmp_path):
    committer = LockedCancellation()
    service = ExportService(work_root=tmp_path / "tasks", committer=committer)
    context = TaskContext()
    try:
        result = service.run(snapshot("# public sample\n\n12345"),
                             OutputIntent.capture(tmp_path / "out.docx"), context)
        assert result.state == TaskState.CANCELLED
        assert [i.code for i in result.issues] == ["EXPORT_CANCELLED", "CLEANUP_PENDING"]
        assert (context.work_dir / ".md2word-task.json").exists()
        assert (context.work_dir / "formatted.docx").exists()
        assert service.pending_cleanup_count == 1
        assert str(tmp_path) not in json.dumps([i.message for i in result.issues])
        assert not service._retained and not (tmp_path / "out.docx").exists()
    finally:
        if committer.guard is not None:
            committer.guard.__exit__(None, None, None)
        service.close()
    assert not context.work_dir.exists()
    assert service.pending_cleanup_count == 0


@pytest.mark.parametrize("action", ["discard", "expire", "evict"])
def test_retained_cleanup_failures_remain_tracked(tmp_path, action):
    service = ExportService(work_root=tmp_path / "tasks")
    service.MAX_RETAINED = 1
    result, artifact = blocked_by_competitor(service, tmp_path)
    try:
        with locked_read(artifact.path):
            if action == "discard":
                assert not service.discard(result.recovery_id)
            elif action == "expire":
                service._retained[result.recovery_id] = replace(artifact, expires_at=0)
                service.expire_retained()
            else:
                blocked_by_competitor(service, tmp_path, "other")
            assert (artifact.task / ".md2word-task.json").exists()
            assert service.pending_cleanup_count == 1
            assert "CLEANUP_PENDING" in [i.code for i in service.drain_diagnostics()]
        service.retry_cleanup()
        assert not artifact.task.exists()
        assert service.pending_cleanup_count == 0
    finally:
        service.close()
    assert not list(service.work_root.iterdir())


def test_pending_cleanup_is_recovered_by_new_service(tmp_path):
    root = tmp_path / "tasks"
    service = ExportService(work_root=root)
    result, artifact = blocked_by_competitor(service, tmp_path)
    second = None
    try:
        with locked_read(artifact.path):
            assert not service.discard(result.recovery_id)
            second = ExportService(work_root=root)
            assert second.pending_cleanup_count == 1
        second.retry_cleanup()
        assert not artifact.task.exists()
        assert second.pending_cleanup_count == 0
        service.retry_cleanup()  # Other instance already removed this owned task.
        assert service.pending_cleanup_count == 0
    finally:
        service.close()
        if second is not None:
            second.close()
    assert not list(root.iterdir())


def test_directory_lock_keeps_last_ownership_marker(tmp_path):
    root = tmp_path / "tasks"
    task, owner = create_task(root)
    (task / "public.txt").write_text("fixed public sample", "utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(task), 0x80000000, 3, None, 3, 0x02000000, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        assert not cleanup_task(task, root, owner)
        assert json.loads((task / ".md2word-task.json").read_text("utf-8"))["owner"] == owner
    finally:
        kernel.CloseHandle(handle)
    assert cleanup_task(task, root, owner)


@pytest.fixture(scope="module")
def math_list_document(tmp_path_factory):
    root = tmp_path_factory.mktemp("verified-math-list")
    return ConversionService().convert_temp(
        DocumentSession.create("3. $$x^2$$\n4. $$y^2$$\n").snapshot(),
        work_root=root, style=resolve_style())


@pytest.mark.parametrize("mutation", ["missing", "level", "start", "format"])
def test_repaired_formula_lists_still_reject_numbering_corruption(tmp_path, math_list_document, mutation):
    result = math_list_document
    with zipfile.ZipFile(result.docx_path) as archive:
        data = {n: archive.read(n) for n in archive.namelist()}
    doc = ET.fromstring(data["word/document.xml"])
    nums = ET.fromstring(data["word/numbering.xml"])
    prop = doc.find(".//w:body/w:p/w:pPr/w:numPr", NS)
    if mutation == "missing":
        prop.getparent().remove(prop)
    elif mutation == "level":
        prop.find(q(W, "ilvl")).set(q(W, "val"), "8")
    else:
        num_id = prop.find(q(W, "numId")).get(q(W, "val"))
        num = next(n for n in nums.findall(q(W, "num")) if n.get(q(W, "numId")) == num_id)
        abstract_id = num.find(q(W, "abstractNumId")).get(q(W, "val"))
        abstract = next(n for n in nums.findall(q(W, "abstractNum"))
                        if n.get(q(W, "abstractNumId")) == abstract_id)
        level = next(n for n in abstract.findall(q(W, "lvl")) if n.get(q(W, "ilvl")) == "0")
        field, value = ("start", "99") if mutation == "start" else ("numFmt", "bullet")
        level.find(q(W, field)).set(q(W, "val"), value)
    data["word/document.xml"] = ET.tostring(doc)
    data["word/numbering.xml"] = ET.tostring(nums)
    corrupted = tmp_path / "corrupt.docx"
    with zipfile.ZipFile(corrupted, "w") as archive:
        for name, raw in data.items():
            archive.writestr(name, raw)
    assert not validate_docx(corrupted, result.checked).passed


def test_cleanup_journal_does_not_replace_task_ownership_check(tmp_path):
    service = ExportService(work_root=tmp_path / "tasks")
    result, artifact = blocked_by_competitor(service, tmp_path)
    marker = artifact.task / ".md2word-task.json"
    try:
        with locked_read(artifact.path):
            assert not service.discard(result.recovery_id)
        marker.write_text(json.dumps({"owner": "not-the-owner", "stage": "05"}), "utf-8")
        assert not service.retry_cleanup()
        assert artifact.path.exists()
        assert service.pending_cleanup_count == 1
        marker.write_text(json.dumps({"owner": artifact.owner, "stage": "05"}), "utf-8")
        assert service.retry_cleanup() and not artifact.task.exists()
    finally:
        service.close()


def test_cleanup_retry_rejects_junction_substituted_for_task(tmp_path):
    import os
    import subprocess
    service = ExportService(work_root=tmp_path / "tasks")
    result, artifact = blocked_by_competitor(service, tmp_path)
    moved = tmp_path / "moved-test-task"
    try:
        with locked_read(artifact.path):
            assert not service.discard(result.recovery_id)
        artifact.task.rename(moved)
        subprocess.run(f'cmd /d /c mklink /J "{artifact.task}" "{moved}"',
                       check=True, capture_output=True)
        try:
            assert not service.retry_cleanup()
            assert (moved / "formatted.docx").exists()
        finally:
            os.rmdir(artifact.task)  # This known junction itself; never recursive.
        moved.rename(artifact.task)
        assert service.retry_cleanup()
    finally:
        service.close()


def test_cleanup_journal_rejects_traversal_record(tmp_path):
    import uuid
    root = tmp_path / "tasks"
    journal = root / ".cleanup-pending"
    journal.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "public.txt"
    sentinel.write_bytes(b"must remain")
    (journal / "forged.json").write_text(json.dumps({
        "schema": 1, "stage": "05", "task": "../outside", "owner": str(uuid.uuid4())}), "utf-8")
    service = ExportService(work_root=root)
    assert service.pending_cleanup_count == 0
    assert [i.code for i in service.drain_diagnostics()] == ["CLEANUP_TRACKING_FAILED"]
    service.close()
    assert sentinel.read_bytes() == b"must remain"


def test_cleanup_record_write_failure_is_reported_and_in_memory_retry_survives(tmp_path, monkeypatch):
    committer = LockedCancellation()
    service = ExportService(work_root=tmp_path / "tasks", committer=committer)
    def cannot_persist(*args):
        raise OSError("injected journal write failure")
    monkeypatch.setattr(service._cleanup, "_save", cannot_persist)
    context = TaskContext()
    try:
        result = service.run(snapshot("# public sample\n\n12345"),
                             OutputIntent.capture(tmp_path / "out.docx"), context)
        assert result.state == TaskState.CANCELLED
        assert [i.code for i in result.issues] == [
            "EXPORT_CANCELLED", "CLEANUP_PENDING", "CLEANUP_TRACKING_FAILED"]
        assert service.pending_cleanup_count == 1
        assert (context.work_dir / ".md2word-task.json").exists()
    finally:
        if committer.guard is not None:
            committer.guard.__exit__(None, None, None)
        service.close()
    assert not context.work_dir.exists()


