"""Fixed stage-four evidence; all inputs are project fixtures, not private files."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from md2word.conversion.service import ConversionService
from md2word.domain.models import InputMode
from md2word.templates.service import resolve_style
from md2word.input import read_input

def main():
    output = ROOT / "artifacts/stage4"
    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / "tests/fixtures/stage2/index.json").read_text("utf-8"))
    cases = [{**case, "subdir": "stage2"} for case in cases]
    cases += [
        {"id":"F06","file":"F06_math.md","subdir":"stage3","mode":"markdown"},
        {"id":"F07","file":"F07_failures.md","subdir":"stage3","mode":"markdown"},
        {"id":"F05","file":"F05_lecture.md","subdir":"stage4","mode":"markdown"},
        {"id":"F18","file":"F18_width_risk.md","subdir":"stage4","mode":"markdown"},
    ]
    service = ConversionService()
    reports = []
    for case in cases:
        overrides = {"body": {"font_zh": "MD2WordUninstalledFont"}} if case["id"] == "F18" else None
        style = resolve_style(overrides=overrides)
        session = read_input(ROOT / "tests/fixtures" / case["subdir"] / case["file"], InputMode(case["mode"]))
        folder = output / case["id"]
        folder.mkdir(exist_ok=True)
        result = service.convert_temp(session.snapshot(), work_root=folder, style=style)
        report = {
            "fixture":case["id"], "docx":str(result.docx_path.relative_to(ROOT)),
            "base_docx":str((result.task_dir / "result.docx").relative_to(ROOT)),
            "sha256":result.sha256, "style_hash":style.resolved_hash,
            "template_version":style.template_version,
            "validation":asdict(result.validation), "elapsed_seconds":result.elapsed_seconds,
            "issues":[{"code":x.code,"severity":x.severity.value,"node_id":x.node_id} for x in result.checked.issues],
            "source":"fixed authored course/structural fixture; no user coursework read",
        }
        assert report["validation"]["passed"]
        if case["id"] == "F18":
            assert {"TABLE_WIDTH_RISK","FONT_MISSING"} <= {i["code"] for i in report["issues"]}
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        reports.append(report)
    payload = {
        "stage":"04","date":"2026-11-01","fixtures":reports,
        "reference":json.loads((ROOT / "resources/templates/lecture-reference.json").read_text("utf-8")),
        "default_style_hash":resolve_style().resolved_hash,
        "note":"Structural/style verification. Word rendering and WPS are reported separately."}
    (output / "verification.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps([{"id":r["fixture"],"passed":r["validation"]["passed"],
                      "issues":[x["code"] for x in r["issues"]]} for r in reports], ensure_ascii=True))

if __name__ == "__main__":
    main()

