"""R2-STD-01: recover the complete journal and never report an incomplete scan as done."""
import json

import pytest

from md2word.exporting.cleanup import CleanupTracker
from md2word.exporting.output import locked_read
from md2word.exporting.service import ExportService
from md2word.exporting.workspace import create_task


def save_failed_cleanup(tracker, root):
    task, owner = create_task(root)
    sample = task / "public.txt"
    sample.write_text("fixed public cleanup fixture", "utf-8")
    # Actual Windows sharing protection causes the deletion failure.
    with locked_read(sample):
        cleaned, issues = tracker.attempt(task, owner)
        assert not cleaned and [i.code for i in issues] == ["CLEANUP_PENDING"]
    return task, owner


@pytest.mark.parametrize("count", [1000, 1001])
def test_all_saved_cleanup_records_are_recovered(tmp_path, count):
    root = tmp_path / "tasks"
    writer = CleanupTracker(root)
    tasks = []
    restored = None
    try:
        for _ in range(count):
            task, _ = save_failed_cleanup(writer, root)
            tasks.append(task)
        assert writer.pending_count == count
        assert len(list((root / writer.JOURNAL).glob("*.json"))) == count
        restored = ExportService(work_root=root)
        assert restored.pending_cleanup_count == count
        assert restored.drain_diagnostics() == ()
        assert restored.retry_cleanup()
        assert restored.pending_cleanup_count == 0
        assert not any(task.exists() for task in tasks)
        assert not (root / writer.JOURNAL).exists()
        assert restored.retry_cleanup()  # Same instance stays consistent.
    finally:
        # All records were created by this writer, including those the buggy
        # loader misses; preserve isolation even on a failing regression.
        if any(task.exists() for task in tasks):
            writer.retry()
        if restored is not None:
            restored.close()
    assert not any(task.exists() for task in tasks)


def test_existing_service_discovers_records_saved_after_construction(tmp_path):
    root = tmp_path / "tasks"
    service = ExportService(work_root=root)
    writer = CleanupTracker(root)
    task, owner = create_task(root)
    sample = task / "public.txt"
    sample.write_bytes(b"fixed public fixture")
    try:
        with locked_read(sample):
            assert not writer.attempt(task, owner)[0]
            assert not service.retry_cleanup()
            assert service.pending_cleanup_count == 1
            assert task.exists() and (task / ".md2word-task.json").exists()
        assert service.retry_cleanup()
        assert service.pending_cleanup_count == 0
        assert not task.exists()
    finally:
        if task.exists():
            writer.retry()
        service.close()


def test_unreadable_record_cannot_report_all_cleanup_complete(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    writer = CleanupTracker(root)
    task, owner = save_failed_cleanup(writer, root)
    original = CleanupTracker._read
    def unreadable(self, path):
        raise OSError("injected journal-read failure")
    # IO injection checks completion semantics; it does not claim a real ACL test.
    monkeypatch.setattr(CleanupTracker, "_read", unreadable)
    service = ExportService(work_root=root)
    try:
        assert not service.retry_cleanup()
        assert service.pending_cleanup_count == 0  # No readable records, NOT completion.
        assert "CLEANUP_TRACKING_FAILED" in [i.code for i in service.drain_diagnostics()]
        assert task.exists()
        assert json.loads((task / ".md2word-task.json").read_text("utf-8"))["owner"] == owner
    finally:
        monkeypatch.setattr(CleanupTracker, "_read", original)
        assert service.retry_cleanup()
        service.close()
    assert not task.exists()

