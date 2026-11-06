"""Generate stage-02 evidence from fixed public fixtures; never reads private user input."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from md2word.domain.models import DocumentError, DocumentSession, InputMode
from md2word.domain.normalizer import normalize
from md2word.domain.repairs import suggest_outer_fence
from md2word.input import MAX_INPUT_BYTES, read_input
from md2word.parsing import Parser

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")

def main():
    parser = Parser()
    fixture_root = ROOT / "tests/fixtures/stage2"
    output = ROOT / "artifacts/stage2"
    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((fixture_root / "index.json").read_text("utf-8"))
    reports = []
    for case in cases:
        session = read_input(fixture_root / case["file"], InputMode(case["mode"]))
        started = time.monotonic()
        parsed = parser.parse(session.snapshot())
        result = normalize(parsed)
        elapsed = time.monotonic() - started
        codes = sorted({i.code for i in result.issues})
        assert codes == sorted(case["issues"]), (case["id"], codes)
        assert result.exportable and result.audit.passed
        folder = output / case["id"]
        folder.mkdir(exist_ok=True)
        write_json(folder / "source.ast.json", parsed.ast.to_dict())
        write_json(folder / "normalized.ast.json", result.ast.to_dict())
        write_json(folder / "manifest.json", [
            {**asdict(e), "payload": e.payload} for e in result.manifest.entries])
        write_json(folder / "source-map.json", [asdict(loc) for loc in parsed.source_map])
        write_json(folder / "transformations.json", [asdict(t) for t in result.fallback_records])
        reports.append({
            "fixture": case["id"], "mode": case["mode"], "elapsed_seconds": elapsed,
            "input_bytes": len(session.current_text.encode("utf-8")),
            "input_key": parsed.input_key, "source_nodes": len(parsed.node_index),
            "normalized_nodes": len(result.node_index),
            "manifest_entries": len(result.manifest.entries),
            "fallbacks": len(result.fallback_records), "issues": codes,
            "repair_available": suggest_outer_fence(session.snapshot()).suggestion is not None,
            "audit": asdict(result.audit),
        })
    measurements = []
    workloads = [
        ("chinese-mixed", ("# 中文资料基准\n\n" +
          "这是用于阶段二解析基准的中文资料，保留数字12345与符号。\n\n" * 300 +
          "\n\n".join("$x_{" + str(i) + "}$" for i in range(30))), InputMode.MARKDOWN),
        ("2MiB-single-paragraph-markdown", "x" * MAX_INPUT_BYTES, InputMode.MARKDOWN),
        ("2MiB-single-paragraph-plain", "x" * MAX_INPUT_BYTES, InputMode.PLAIN),
        ("many-atoms-guard", "a " * 60_000, InputMode.MARKDOWN),
        ("deep-nesting-guard", ">" * 110 + " deep\n", InputMode.MARKDOWN),
    ]
    for label, text, mode in workloads:
        tracemalloc.start()
        started = time.monotonic()
        try:
            parsed = parser.parse(DocumentSession.create(text, mode).snapshot(),
                                  deadline=time.monotonic() + 15)
            result = normalize(parsed)
            status = "passed" if result.exportable else "blocked"
            nodes = len(parsed.node_index)
        except DocumentError as exc:
            status, nodes = exc.code, None
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        measurements.append({
            "workload": label, "characters": len(text),
            "utf8_bytes": len(text.encode("utf-8")), "mode": mode.value,
            "status": status, "nodes": nodes,
            "elapsed_seconds_with_tracemalloc": elapsed,
            "peak_python_heap_bytes": peak,
        })
    assert all(m["status"] == "passed" for m in measurements[:3])
    assert all(m["status"] == "INPUT_COMPLEXITY" for m in measurements[3:])
    report = {
        "stage": "02", "date": "2026-11-01",
        "engine": parser.profile.engine_version, "ast_api": parser.profile.ast_api_version,
        "reader_fingerprint": parser.profile.fingerprint, "policy_version": "support-v1",
        "fixtures": reports, "measurements": measurements,
        "limits": {"utf8_bytes": MAX_INPUT_BYTES, "ast_tagged_nodes": 100_000,
                   "json_container_depth": 96},
        "notes": [
            "Real Pandoc for Markdown, direct literal AST for plain text.",
            "No DOCX exported; math status remains unchecked.",
            "Tracemalloc measures Python allocations, not total process/child memory.",
            "Two MiB input allowance is not a promise that all complex inputs of that size pass.",
        ],
    }
    write_json(output / "verification.json", report)
    print(json.dumps({"fixtures": len(reports), "measurements": measurements},
                     ensure_ascii=True, indent=2))

if __name__ == "__main__":
    main()

