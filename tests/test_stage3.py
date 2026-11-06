from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import zipfile

from lxml import etree as ET
import pytest

from md2word.conversion.math_check import (
    MathCache, MathChecker, ProbeOutcome, audit_checked, extract_math)
from md2word.conversion.ooxml import M, NS, P, W, Package, q
from md2word.conversion.service import ConversionService
from md2word.conversion.validation import validate_docx
from md2word.conversion.writer import DocxWriter
from md2word.domain.conversion_models import MathStatus
from md2word.domain.models import DocumentError, DocumentSession, FrozenAst, InputMode
from md2word.domain.normalizer import normalize
from md2word.input import read_input
from md2word.parsing import Parser

ROOT = Path(__file__).parent / "fixtures"
CASES = json.loads((ROOT / "stage2/index.json").read_text("utf-8"))

@pytest.fixture(scope="module")
def service():
    return ConversionService()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_real_normalized_fixtures_write_and_validate(service, tmp_path, case):
    source = ROOT / "stage2" / case["file"]
    before = source.read_bytes()
    session = read_input(source, InputMode(case["mode"]))
    result = service.convert_temp(session.snapshot(), work_root=tmp_path / "中文 临时目录")
    assert result.validation.passed
    assert result.docx_path.parent == result.task_dir
    assert result.docx_path.name == "result.docx"
    assert result.checked.normalized.parsed.snapshot.text == session.original_text
    assert source.read_bytes() == before
    assert sorted(p.name for p in result.task_dir.iterdir()) == [".md2word-task.json", "result.docx"]
    assert validate_docx(result.docx_path, result.checked).passed
    if case["id"] == "F09":
        package = Package.read(result.docx_path)
        links = package.document.findall(".//w:hyperlink", NS)
        assert package.hyperlink(links[0]) == ("https://example.com/path?q=123", "标题 456")

@pytest.mark.parametrize("fixture", ["F06_math.md", "F07_failures.md"])
def test_math_cases_real_docx(service, tmp_path, fixture):
    session = read_input(ROOT / "stage3" / fixture, InputMode.MARKDOWN)
    snapshot = session.snapshot()
    result = service.convert_temp(snapshot, work_root=tmp_path)
    rows = result.checked.math_records
    assert result.validation.passed and rows
    assert all(r.status in {MathStatus.STRUCTURALLY_PASSED, MathStatus.SOURCE_FALLBACK} for r in rows)
    assert [r.ordinal for r in rows] == list(range(1, len(rows) + 1))
    assert result.validation.math_objects == sum(r.status == MathStatus.STRUCTURALLY_PASSED for r in rows)
    assert not list(result.task_dir.glob("probe-*"))
    assert result.checked.cache_hits >= 1
    assert result.checked.normalized.parsed.snapshot == snapshot
    if fixture.startswith("F06"):
        assert all(r.status == MathStatus.STRUCTURALLY_PASSED for r in rows)
        assert len({r.cache_key for r in rows if r.source == "x^2+y_1"}) == 2
    else:
        fallback = [r for r in rows if r.status == MathStatus.SOURCE_FALLBACK]
        assert len(fallback) == 2
        assert sum(i.code == "MATH_FALLBACK" for i in result.checked.issues) == 2
        body = Package.read(result.docx_path).document
        text = "".join(body.itertext())
        assert "$not_closed" in text and "$20" in text and "$30" in text
        assert r"\unknownmdtw{7}" in text and r"\frac{1}{" in text
        assert len(result.checked.math_fallbacks) == 2

def test_cache_scope_revision_types_and_eviction(tmp_path):
    parser = Parser()
    checker = MathChecker(parser.runner, MathCache(capacity=2))
    session = DocumentSession.create("$x$ 与 $x$。\n\n$$x$$\n")
    doc = normalize(parser.parse(session.snapshot()))
    checked = checker.check_math(doc, work_dir=tmp_path)
    assert checked.probes == 2 and checked.cache_hits == 1
    edited = session.edit(session.current_text + "\n\n补充 123。\n")
    newer = checker.check_math(normalize(parser.parse(edited.snapshot())), work_dir=tmp_path)
    assert newer.probes == 0 and newer.cache_hits == 3
    assert all(r.location.revision == edited.content_revision for r in newer.math_records)
    # Same text in a new session does not share cache values.
    other = checker.check_math(normalize(parser.parse(DocumentSession.create(session.current_text).snapshot())),
                               work_dir=tmp_path)
    assert other.probes == 2 and other.cache_hits == 1
    extra_session = DocumentSession.create("$a$ $b$ $c$")
    checker.check_math(normalize(parser.parse(extra_session.snapshot())), work_dir=tmp_path)
    assert len(checker.cache) == 2

def test_empty_formula_uncertain_never_written(monkeypatch, tmp_path):
    parser = Parser()
    session = DocumentSession.create("$x$")
    ast = parser.parse(session.snapshot()).ast.to_dict()
    ast["blocks"][0]["c"][0]["c"][1] = ""
    with monkeypatch.context() as patch:
        patch.setattr(parser.runner, "parse", lambda *a, **kw: ast)
        doc = normalize(parser.parse(session.snapshot()))
    checked = MathChecker(parser.runner).check_math(doc, work_dir=tmp_path)
    assert checked.math_records[0].status == MathStatus.UNCERTAIN
    assert not checked.ready
    with pytest.raises(DocumentError) as exc:
        DocxWriter(parser.runner).write_docx(checked, work_dir=tmp_path)
    assert exc.value.code == "MATH_UNCERTAIN"
    assert not (tmp_path / "result.docx").exists()

def test_shared_budget_and_cancel_with_probe_fault_injection(monkeypatch, tmp_path):
    parser = Parser()
    doc = normalize(parser.parse(DocumentSession.create("$a$ $b$ $c$").snapshot()))
    checker = MathChecker(parser.runner)
    received = []
    def controlled_probe(record, profile, reference, work_dir, deadline, cancel):
        received.append(deadline)
        time.sleep(0.07)
        return ProbeOutcome(MathStatus.STRUCTURALLY_PASSED, "test-signature", ())
    monkeypatch.setattr(checker, "probe", controlled_probe)
    deadline = time.monotonic() + 0.1
    with pytest.raises(DocumentError) as exc:
        checker.check_math(doc, work_dir=tmp_path, deadline=deadline)
    assert exc.value.code == "EXPORT_TIMEOUT"
    assert received and set(received) == {deadline}
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DocumentError) as exc:
        checker.check_math(doc, work_dir=tmp_path, cancel=cancel)
    assert exc.value.code == "EXPORT_CANCELLED"

@pytest.fixture(scope="module")
def corruption_base(tmp_path_factory):
    folder = tmp_path_factory.mktemp("stage3-base")
    source = ("# 标题 2026\n\n正文 12345。链接 [示例](https://example.com/a \"标题 789\")。\n\n"
              "3. 项目 300\n4. 项目 400\n\n"
              "| 名称 | 数值 |\n|---|---|\n| A | |\n\n"
              + chr(96) * 3 + "python\n    print(6789)\n" + chr(96) * 3
              + "\n\n公式 $x^2$ 与 $y_1$。\n")
    return ConversionService().convert_temp(DocumentSession.create(source).snapshot(), work_root=folder)

def mutate_zip(source, target, mutate):
    with zipfile.ZipFile(source) as package:
        data = {i.filename: package.read(i.filename) for i in package.infolist()}
    mutate(data)
    with zipfile.ZipFile(target, "x") as package:
        for name, content in data.items():
            package.writestr(name, content)

@pytest.mark.parametrize("kind", [
    "number", "code_indent", "empty_cell", "table_row", "link_target", "heading",
    "swap_math", "math_structure", "math_removed", "numbering_start",
    "dangling_relationship", "missing_part", "wrong_content_type", "unknown_content",
])
def test_validator_rejects_corrupt_docx(corruption_base, tmp_path, kind):
    base = corruption_base
    def change(data):
        if kind in {"dangling_relationship", "link_target"}:
            rel = ET.fromstring(data["word/_rels/document.xml.rels"])
            node = next(x for x in rel if x.get("Type", "").endswith("/hyperlink"))
            if kind == "dangling_relationship":
                node.set("Id", "changed-id")
            else:
                node.set("Target", "https://example.com/changed")
            data["word/_rels/document.xml.rels"] = ET.tostring(rel)
            return
        if kind == "missing_part":
            data.pop("word/styles.xml")
            return
        if kind == "wrong_content_type":
            data["[Content_Types].xml"] = data["[Content_Types].xml"].replace(
                b"wordprocessingml.document.main+xml", b"wordprocessingml.document.fake+xml")
            return
        if kind == "numbering_start":
            root = ET.fromstring(data["word/numbering.xml"])
            for el in root.findall(".//w:startOverride", NS):
                el.set(q(W, "val"), "99")
            data["word/numbering.xml"] = ET.tostring(root)
            return
        root = ET.fromstring(data["word/document.xml"])
        if kind == "number":
            node = next(x for x in root.findall(".//w:t", NS) if "12345" in (x.text or ""))
            node.text = node.text.replace("12345", "12346")
        elif kind == "code_indent":
            node = next(x for x in root.findall(".//w:t", NS) if "    print" in (x.text or ""))
            node.text = node.text.lstrip()
        elif kind == "empty_cell":
            cell = next(x for x in root.findall(".//w:tc", NS) if not "".join(x.itertext()))
            ET.SubElement(ET.SubElement(cell.find(q(W, "p")), q(W, "r")), q(W, "t")).text = "injected"
        elif kind == "table_row":
            row = root.findall(".//w:tr", NS)[-1]
            row.getparent().remove(row)
        elif kind == "heading":
            root.find(".//w:pStyle", NS).set(q(W, "val"), "BodyText")
        elif kind == "swap_math":
            a, b = root.findall(".//m:oMath", NS)
            pa, pb = a.getparent(), b.getparent()
            ia, ib = pa.index(a), pb.index(b)
            aa, bb = copy.deepcopy(a), copy.deepcopy(b)
            pa.replace(a, bb)
            pb.replace(b, aa)
        elif kind == "math_structure":
            sup = root.find(".//m:sSup", NS)
            sup.tag = q(M, "sSub")
            sup.find(q(M, "sup")).tag = q(M, "sub")
        elif kind == "math_removed":
            node = root.find(".//m:oMath", NS)
            node.getparent().remove(node)
        elif kind == "unknown_content":
            ET.SubElement(root.find(".//w:r", NS), q(W, "delText")).text = "hidden 123"
        data["word/document.xml"] = ET.tostring(root)
    path = tmp_path / (kind + ".docx")
    mutate_zip(base.docx_path, path, change)
    report = validate_docx(path, base.checked)
    assert not report.passed
    assert report.issues[0].code in {"CONTENT_MISMATCH", "DOCX_INVALID"}

def test_failures_do_not_touch_existing_files_and_task_cleanup(tmp_path):
    root = tmp_path / "工作目录"
    root.mkdir()
    important = root / "important.docx"
    important.write_bytes(b"original-user-file")
    service = ConversionService()
    with pytest.raises(DocumentError):
        service.convert_temp(DocumentSession.create("").snapshot(), work_root=root)
    assert important.read_bytes() == b"original-user-file"
    assert not list(root.glob("job-*"))

def test_math_fallback_audit_blocks_undeclared_change(service, tmp_path):
    session = DocumentSession.create(r"$\unknownmdtw{42}$")
    result = service.convert_temp(session.snapshot(), work_root=tmp_path)
    damaged = result.checked.ast.to_dict()
    damaged["blocks"][0]["c"][0]["c"][1] = "deleted 42"
    with pytest.raises(DocumentError) as exc:
        audit_checked(replace(result.checked, ast=FrozenAst.capture(damaged)))
    assert exc.value.code == "CONTENT_MISMATCH"

@pytest.mark.parametrize("text,mode", [
    ("$$\n\\unknownmdtw{123}\n$$\n", InputMode.MARKDOWN),
    (chr(96)*3 + "text\n\tline 123\n    second\n" + chr(96)*3, InputMode.MARKDOWN),
    ("A\r\n\r\n\r\nB\r\n", InputMode.PLAIN),
], ids=["multiline-fallback", "code-tab", "plain-empty-paragraph"])
def test_multiline_content_is_preserved(service, tmp_path, text, mode):
    result = service.convert_temp(DocumentSession.create(text, mode).snapshot(), work_root=tmp_path)
    assert result.validation.passed
    if "unknownmdtw" in text:
        assert len(result.checked.math_fallbacks) == 1

def test_writer_never_overwrites_existing_task_files(service, tmp_path):
    doc = normalize(service.parser.parse(DocumentSession.create("test 123").snapshot()))
    checked = service.checker.check_math(doc, work_dir=tmp_path)
    log = tmp_path / "pandoc-log.json"
    log.write_bytes(b"preexisting-file")
    with pytest.raises(DocumentError) as exc:
        service.writer.write_docx(checked, work_dir=tmp_path)
    assert exc.value.code == "OUTPUT_CONFLICT"
    assert log.read_bytes() == b"preexisting-file"

def test_unexpected_final_writer_warning_blocks_output(monkeypatch, tmp_path):
    service = ConversionService()
    original = service.parser.runner.write_docx
    def with_warning(*args, **kwargs):
        result = original(*args, **kwargs)
        log = kwargs["cwd"] / "pandoc-log.json"
        entries = json.loads(log.read_text("utf-8"))
        entries.append({"type": "UnknownWriterWarning", "verbosity": "WARNING"})
        log.write_text(json.dumps(entries), "utf-8")
        return result
    monkeypatch.setattr(service.parser.runner, "write_docx", with_warning)
    with pytest.raises(DocumentError) as exc:
        service.convert_temp(DocumentSession.create("text 123").snapshot(), work_root=tmp_path)
    assert exc.value.code == "WRITER_UNCERTAIN"
    assert not list(tmp_path.glob("job-*"))

def test_number_label_change_is_not_accepted(corruption_base, tmp_path):
    def change(data):
        numbering = ET.fromstring(data["word/numbering.xml"])
        for node in numbering.findall(".//w:lvlText", NS):
            if (node.get(q(W, "val")) or "").startswith("%"):
                node.set(q(W, "val"), "WRONG" + node.get(q(W, "val")))
        data["word/numbering.xml"] = ET.tostring(numbering)
    path = tmp_path / "bad-label.docx"
    mutate_zip(corruption_base.docx_path, path, change)
    assert not validate_docx(path, corruption_base.checked).passed

