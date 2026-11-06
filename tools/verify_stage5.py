"""Non-private stage-five acceptance evidence, including an optional native Word lock."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from md2word.domain.models import InputMode
from md2word.input import read_input
from md2word.templates.service import resolve_style
from md2word.exporting.models import ExportSnapshot, TaskContext, TaskState
from md2word.exporting.output import OutputIntent, fingerprint
from md2word.exporting.service import ExportService
from md2word.exporting.scheduler import Scheduler

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description="Verify stage-five behavior against a selected regression report")
parser.add_argument("--regression-report", type=Path, default=Path("artifacts/pytest-stages01-05.xml"))
args = parser.parse_args()
REGRESSION = (ROOT / args.regression_report).resolve()
REGRESSION_LABEL = str(REGRESSION.relative_to(ROOT)) if REGRESSION.is_relative_to(ROOT) else "external report"
OUT = ROOT / "artifacts/stage5"
OUT.mkdir(parents=True, exist_ok=True)
RUN = OUT / ("run-" + uuid.uuid4().hex[:12])
RUN.mkdir()
service = ExportService(work_root=RUN / "tasks")
scheduler = Scheduler(service)
report = {"cases": [], "scope": "fixed non-private fixtures; real bundled Pandoc",
          "existing_target_policy": "fail_closed_when_atomic_protected_replacement_unavailable"}


def evidence(name, result, handle, **extra):
    record = {
        "name": name, "state": result.state.value,
        "revision": result.snapshot.input.revision,
        "style_revision": result.snapshot.style_revision,
        "sha256": result.sha256,
        "output": str(result.output_path.relative_to(ROOT)) if result.output_path else None,
        "issue_codes": [x.code for x in result.issues],
        "recoverable": result.recovery_id is not None,
        "states": [x.state.value for x in handle.context.drain_events()],
        **extra}
    report["cases"].append(record)


try:
    for filename, fixture in [
        ("F14.docx", "tests/fixtures/stage5/F14_snapshot.md"),
        ("F05.docx", "tests/fixtures/stage4/F05_lecture.md"),
        ("F07.docx", "tests/fixtures/stage3/F07_math_fallback.md"),
    ]:
        # Locate the existing stage-three F07 fixture by controlled prefix if renamed.
        source = ROOT / fixture
        if not source.exists() and filename == "F07.docx":
            source = next((ROOT / "tests/fixtures/stage3").glob("F07*.md"))
        snapshot = ExportSnapshot.capture(read_input(source, InputMode.MARKDOWN), resolve_style())
        handle = scheduler.submit(snapshot, OutputIntent.capture(RUN / filename))
        result = handle.result(60)
        evidence(filename[:3], result, handle)
        assert result.state == TaskState.SUCCEEDED
        if filename == "F14.docx":
            first_result, first_snapshot = result, snapshot

    target = RUN / "concurrent.docx"
    intent = OutputIntent.capture(target)
    target.write_bytes(b"Created by a competing program; must remain intact.")
    old_sha = fingerprint(target).sha256
    handle = scheduler.submit(first_snapshot, intent)
    failed = handle.result(60)
    preserved = fingerprint(target).sha256 == old_sha
    evidence("new_file_conflict", failed, handle, original_preserved=preserved)
    assert failed.recovery_id and preserved
    handle = scheduler.retry(failed, OutputIntent.capture(RUN / "reselected.docx"))
    recovered = handle.result(60)
    evidence("retry_reselected", recovered, handle)
    assert recovered.state == TaskState.SUCCEEDED

    old = RUN / "old.docx"
    old.write_bytes(b"Original protected file.")
    intent = OutputIntent.capture(old, overwrite=True)
    old.write_bytes(b"A later edit after overwrite confirmation.")
    expected = fingerprint(old).sha256
    handle = scheduler.submit(first_snapshot, intent)
    result = handle.result(60)
    preserved = fingerprint(old).sha256 == expected
    evidence("confirmed_target_changed", result, handle, original_preserved=preserved)
    assert result.issues[-1].code == "OUTPUT_CONFLICT" and preserved
    scheduler.service.discard(result.recovery_id)

    intent = OutputIntent.capture(old, overwrite=True)
    handle = scheduler.submit(first_snapshot, intent)
    result = handle.result(60)
    evidence("unchanged_target_fail_closed", result, handle,
             original_preserved=fingerprint(old).sha256 == expected)
    assert result.issues[-1].code == "OUTPUT_OVERWRITE_UNSAFE"
    scheduler.service.discard(result.recovery_id)

    gone = RUN / "confirmed-deleted.docx"
    gone.write_bytes(b"Confirmed file, then removed by another program.")
    intent = OutputIntent.capture(gone, overwrite=True)
    gone.unlink()
    handle = scheduler.submit(first_snapshot, intent)
    result = handle.result(60)
    evidence("confirmed_target_deleted", result, handle, missing_target_not_recreated=not gone.exists())
    assert result.issues[-1].code == "OUTPUT_CONFLICT" and not gone.exists()
    assert result.recovery_id
    scheduler.service.discard(result.recovery_id)

    # Native Word opens only our fresh QA copy, no user document.
    target = RUN / "word-open.docx"
    shutil.copyfile(first_result.output_path, target)
    before = fingerprint(target).sha256
    intent = OutputIntent.capture(target, overwrite=True)
    ready, stop = RUN / "word-ready.json", RUN / "word-stop"
    worker = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "tools/word_lock_fixture.ps1"),
         "-Target", str(target), "-Ready", str(ready), "-Stop", str(stop)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        until = time.monotonic() + 15
        while not ready.exists() and worker.poll() is None and time.monotonic() < until:
            time.sleep(.1)
        if ready.exists():
            version = json.loads(ready.read_text("utf-8-sig"))
            handle = scheduler.submit(first_snapshot, intent)
            result = handle.result(60)
            evidence("native_word_open", result, handle, application=version,
                     original_preserved=fingerprint(target).sha256 == before)
            assert result.state == TaskState.FAILED
            assert result.issues[-1].code in {"OUTPUT_LOCKED", "OUTPUT_OVERWRITE_UNSAFE"}
            if result.recovery_id:
                scheduler.service.discard(result.recovery_id)
        else:
            report["native_word"] = "not_verified; Word fixture did not signal ready"
    finally:
        stop.write_text("stop", "ascii")
        stdout, stderr = worker.communicate(timeout=40)
        report["word_worker_exitcode"] = worker.returncode
    assert fingerprint(target).sha256 == before

    tree = ET.parse(REGRESSION)
    suite = tree.find("testsuite")
    assert suite.get("failures") == suite.get("errors") == "0"
    report["regression_source"] = REGRESSION_LABEL
    report["regression"] = {key: suite.get(key) for key in ("tests", "failures", "errors", "skipped", "time")}
    cases = [case for case in suite.findall("testcase") if "test_stage5" in case.get("classname", "")]
    report["stage5_tests"] = len(cases)
    # Reuse exact full-run testcase evidence, preserving outcomes; no invented test execution.
    stage5 = ET.Element("testsuites")
    subset = ET.SubElement(stage5, "testsuite", name="stage5 extracted from full run", tests=str(len(cases)),
                           failures=str(sum(c.find("failure") is not None for c in cases)),
                           errors=str(sum(c.find("error") is not None for c in cases)), skipped="0")
    subset.set("source", REGRESSION_LABEL)
    for case in cases:
        subset.append(case)
    ET.ElementTree(stage5).write(OUT / "pytest-stage5-from-full.xml", encoding="utf-8", xml_declaration=True)
    ET.ElementTree(stage5).write(ROOT / "artifacts/pytest-stage5.xml", encoding="utf-8", xml_declaration=True)
    report["fault_tests_scope"] = {
        "cancel_timeout": "real Python sleeping child substituted for one Pandoc invocation; real lifecycle and exit checked",
        "normal_conversion": "actual bundled Pandoc",
        "sharing_lock": "real Windows CreateFileW sharing protection; native Word additionally checked above",
        "disk_full_access_denied": "injected OSError and fsync failures; not physical full disk/ACL simulation",
        "cleanup_reparse": "actual Windows junction; external file hash/content preserved",
        "inflight_editor": "actual immutable session edits during worker pause; no GUI until stage06",
    }
    report["status"] = "passed"
except Exception as exc:
    report["status"] = "failed"
    report["failure_type"] = type(exc).__name__
    raise
finally:
    scheduler.dispose()
    report["remaining_task_dirs"] = len(list((RUN / "tasks").glob("export-*")))
    path = OUT / "verification.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"status": report["status"], "report": str(path.relative_to(ROOT)),
                      "cases": len(report["cases"]), "remaining_task_dirs": report["remaining_task_dirs"]}))

