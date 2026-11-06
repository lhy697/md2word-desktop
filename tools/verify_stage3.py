"""Real fixed-fixture conversion evidence. Does not read private documents."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from md2word.conversion.service import ConversionService
from md2word.domain.models import InputMode
from md2word.input import read_input

def main():
    output = ROOT / "artifacts/stage3"
    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / "tests/fixtures/stage2/index.json").read_text("utf-8"))
    cases = [{**case, "subdir": "stage2"} for case in cases]
    cases += [
        {"id": "F06", "file": "F06_math.md", "mode": "markdown", "subdir": "stage3"},
        {"id": "F07", "file": "F07_failures.md", "mode": "markdown", "subdir": "stage3"},
    ]
    service = ConversionService()
    reports = []
    for case in cases:
        root = output / case["id"]
        root.mkdir(exist_ok=True)
        session = read_input(ROOT / "tests/fixtures" / case["subdir"] / case["file"], InputMode(case["mode"]))
        result = service.convert_temp(session.snapshot(), work_root=root)
        report = {
            "fixture": case["id"], "task": str(result.task_dir.relative_to(ROOT)),
            "docx": str(result.docx_path.relative_to(ROOT)), "sha256": result.sha256,
            "elapsed_seconds": result.elapsed_seconds, "validation": asdict(result.validation),
            "math": [asdict(r) for r in result.checked.math_records],
            "probes": result.checked.probes, "cache_hits": result.checked.cache_hits,
            "issues": [{"code": x.code, "severity": x.severity.value,
                        "node_id": x.node_id} for x in result.checked.issues],
            "math_fallbacks": [asdict(x) for x in result.checked.math_fallbacks],
        }
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        reports.append(report)
    complete = {"stage": "03", "date": "2026-11-01", "fixtures": reports,
                "engine": service.parser.profile.engine_version,
                "reference": json.loads((ROOT / "resources/templates/reference-base.json").read_text("utf-8")),
                "note": "Temporary DOCX structural/content verification; not publication or user-target commit."}
    (output / "verification.json").write_text(json.dumps(complete, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps([{
        "fixture": r["fixture"], "passed": r["validation"]["passed"],
        "math_objects": r["validation"]["math_objects"], "probes": r["probes"],
        "fallbacks": len(r["math_fallbacks"]), "elapsed": round(r["elapsed_seconds"], 3),
    } for r in reports], ensure_ascii=True, indent=2))

if __name__ == "__main__":
    main()
