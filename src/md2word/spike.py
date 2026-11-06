"""Development experiments, not production normalization or content validation."""
import copy
import json
from pathlib import Path
import tempfile
import time
import zipfile
from xml.etree import ElementTree as ET
from .pandoc import PandocRunner
from .resources import ResourceLocator

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
      "m": "http://schemas.openxmlformats.org/officeDocument/2006/math"}

def inspect_docx(path: Path) -> dict:
    with zipfile.ZipFile(path) as package:
        if package.testzip():
            raise ValueError("DOCX CRC failure")
        for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            ET.fromstring(package.read(name))
        doc = ET.fromstring(package.read("word/document.xml"))
    return {
        "text": "".join(doc.itertext()),
        "headings": [x.get(f"{{{NS['w']}}}val") for x in
                     doc.findall(".//w:pStyle", NS) if
                     (x.get(f"{{{NS['w']}}}val") or "").startswith("Heading")],
        "tables": len(doc.findall(".//w:tbl", NS)),
        "math_objects": len(doc.findall(".//m:oMath", NS)),
        "list_paragraphs": len(doc.findall(".//w:numPr", NS)),
    }

def smoke(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite experiment results, including a user's existing file.
    target = output_dir / "smoke.docx"
    if target.exists():
        raise FileExistsError("Smoke result exists; choose a fresh output directory")
    started = time.monotonic()
    runner = PandocRunner()
    with tempfile.TemporaryDirectory(prefix="md2word-") as tmp:
        work = Path(tmp)
        ast = runner.parse(ResourceLocator().file("examples/smoke.md").read_text("utf-8"), cwd=work)
        result = runner.write_docx(ast, work / "smoke.docx", cwd=work)
        report = inspect_docx(work / "smoke.docx")
        expected = ["复习讲义", "2026", "3.14159", "速度", "距离", "print", "return"]
        if not all(x in report["text"] for x in expected):
            raise ValueError("Smoke content missing")
        if not (len(report["headings"]) == 3 and report["tables"] == 1
                and report["math_objects"] == 4 and report["list_paragraphs"] >= 2):
            raise ValueError(f"Smoke structure mismatch: {report}")
        # Exclusive creation protects a pre-existing target. Not stage-05 atomic commit.
        with target.open("xb") as destination:
            destination.write((work / "smoke.docx").read_bytes())
        (output_dir / "smoke.ast.json").write_text(
            json.dumps(ast, ensure_ascii=False, indent=2), "utf-8")
        report["pandoc_log"] = json.loads((work / "pandoc-log.json").read_text("utf-8"))
        report["writer_seconds"] = result.elapsed_seconds
    report["elapsed_seconds"] = time.monotonic() - started
    report["note"] = "Structural spike only; Word/WPS display and editing not verified"
    (output_dir / "smoke-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report

def math_probe(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    runner = PandocRunner()
    cases = [
        ("InlineMath", r"x^2 + y_1"),
        ("DisplayMath", r"\frac{1}{2}+\sqrt{x}"),
        ("DisplayMath", r"\sum_{i=1}^{n}i+\int_0^1 x\,dx"),
        ("DisplayMath", r"\begin{pmatrix}1&2\\3&4\end{pmatrix}"),
        ("InlineMath", r"\unknownmdtw{z}"),
        ("InlineMath", r"x^2 + y_1"),
        ("DisplayMath", r"x^2 + y_1"),
        ("DisplayMath", r"\frac{1}{"),
        ("InlineMath", ""),
    ]
    base = runner.parse("", cwd=output_dir)
    def probe_ast(items):
        ast = copy.deepcopy(base)
        ast["blocks"] = [{"t": "Para", "c": [
            {"t": "Str", "c": f"PROBE_{i:03d}"},
            {"t": "Space"},
            {"t": "Math", "c": [{"t": kind}, src]}
        ]} for i, (kind, src) in items]
        return ast

    deadline = time.monotonic() + 60
    all_items = list(enumerate(cases))
    runner.write_docx(probe_ast(all_items), output_dir / "batch.docx",
                      cwd=output_dir, deadline=deadline)
    batch_log = json.loads((output_dir / "pandoc-log.json").read_text("utf-8"))
    with zipfile.ZipFile(output_dir / "batch.docx") as z:
        root = ET.fromstring(z.read("word/document.xml"))
    batch = []
    for p in root.findall(".//w:body/w:p", NS):
        text = "".join(p.itertext())
        marker = next((f"PROBE_{i:03d}" for i in range(len(cases))
                       if f"PROBE_{i:03d}" in text), None)
        batch.append({"marker": marker, "omml_count": len(p.findall(".//m:oMath", NS)),
                      "text": text})
    singles = []
    for i, case in all_items:
        folder = output_dir / f"single-{i:03d}"
        folder.mkdir()
        result = runner.write_docx(probe_ast([(i, case)]), folder / "probe.docx",
                                  cwd=folder, deadline=deadline)
        checked = inspect_docx(folder / "probe.docx")
        logs = json.loads((folder / "pandoc-log.json").read_text("utf-8"))
        warning = bool(logs)
        state = ("source_fallback" if warning and checked["math_objects"] == 0
                 and case[1] in checked["text"] else
                 "structurally_passed" if case[1].strip() and not warning and checked["math_objects"] == 1
                 else "uncertain")
        singles.append({"ordinal": i, "type": case[0], "source": case[1],
                        "returncode": result.returncode,
                        "omml_count": checked["math_objects"],
                        "log": logs, "text": checked["text"], "status": state})
    report = {"batch": batch, "batch_log": batch_log, "singles": singles,
              "note": "Experimental evidence only; no production fallback or semantic guarantee"}
    (output_dir / "math-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report
