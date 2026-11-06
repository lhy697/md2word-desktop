from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import copy
import zipfile
import pytest
from lxml import etree as ET
from pydantic import ValidationError
from md2word.domain.models import DocumentError, DocumentSession, InputMode
from md2word.domain.style_models import StyleConfig, ResolvedStyle, mm_to_twips
from md2word.templates.service import PersonalStyleRepository, resolve_style
from md2word.templates.fonts import check_fonts, system_fonts
from md2word.input import read_input
from md2word.conversion.service import ConversionService
from md2word.conversion.postprocess import postprocess, object_snapshot, assert_objects
from md2word.conversion.ooxml import Package, NS, W, q
from md2word.conversion.validation import validate_docx

ROOT = Path(__file__).parent / "fixtures"
CASES = json.loads((ROOT / "stage2/index.json").read_text("utf-8"))
CASES += [{"id": "F06", "file": "../stage3/F06_math.md", "mode": "markdown"},
          {"id": "F07", "file": "../stage3/F07_failures.md", "mode": "markdown"}]

@pytest.fixture(scope="module")
def service():
    return ConversionService()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_styled_fixtures_keep_contents_and_objects(service, tmp_path, case):
    session = read_input(ROOT / "stage2" / case["file"], InputMode(case["mode"]))
    snapshot = session.snapshot()
    style = resolve_style()
    result = service.convert_temp(snapshot, style=style, work_root=tmp_path)
    assert result.validation.passed and result.style == style
    before, after = Package.read(result.task_dir / "result.docx"), Package.read(result.docx_path)
    assert_objects(object_snapshot(before), after)
    assert validate_docx(result.docx_path, result.checked).passed
    assert result.checked.normalized.parsed.snapshot == snapshot
    assert after.document.find(".//w:pgSz", NS).get(q(W, "w")) == str(mm_to_twips(210))
    assert after.document.find(".//w:pgMar", NS).get(q(W, "left")) == str(mm_to_twips(25))
    footer = next(root for name, root in after.parts.items() if "md2word-footer" in name)
    assert footer.find(".//w:fldSimple", NS).get(q(W, "instr")).strip() == "PAGE"

@pytest.mark.parametrize("patch", [
    {"body": {"size_pt": -1}},
    {"body": {"size_pt": "12"}},
    {"body": {"size_pt": float("nan")}},
    {"body": {"line_spacing": {"mode": "fixed", "points": 18, "multiplier": 1.5}}},
    {"body": {"line_spacing": {"mode": "multiple", "points": 18}}},
    {"page": {"width_mm": 10}},
    {"page": {"width_mm": 140, "left_mm": 60, "right_mm": 60}},
    {"unknown": True},
    {"body": {"font_zh": "C:\\private\\font.ttf"}},
    {"schema_version": 2},
    {"headings": []},
], ids=["negative-size","string-size","nan","mixed-line-mode","missing-multiplier",
        "paper-size","unusable-area","unknown-field","font-path","future-schema","headings"])
def test_invalid_styles_cannot_reach_conversion(patch):
    with pytest.raises(DocumentError):
        resolve_style(overrides=patch)

def test_resolved_snapshot_hash_and_config_validation():
    a = resolve_style()
    b = resolve_style(overrides={"body": {"size_pt": 14}})
    assert a.resolved_hash != b.resolved_hash
    assert a == resolve_style()
    with pytest.raises(ValidationError):
        a.config.body.size_pt = 99
    with pytest.raises(FrozenInstanceError):
        a.resolved_hash = "bad"
    raw = a.config.model_dump(mode="json")
    raw["body"].pop("size_pt")
    with pytest.raises(DocumentError):
        StyleConfig.parse_config(raw)
    assert a.config.body.size_pt == 12

def test_personal_style_crud_corruption_future_versions_and_builtin_protection(tmp_path):
    repo = PersonalStyleRepository(tmp_path / "styles")
    base = resolve_style()
    personal = repo.save(base.config, name="我的讲义")
    identity = personal.id
    assert repo.load(identity) == personal
    assert repo.rename(identity, "课程复习").id == identity
    override = resolve_style(personal=repo.load(identity), overrides={"body": {"first_line_chars": 1}})
    assert override.config.name == "课程复习" and override.config.body.first_line_chars == 1
    assert repo.restore_builtin().config.id == "lecture"
    with pytest.raises(DocumentError):
        repo.delete("lecture")
    with pytest.raises(DocumentError):
        repo.save(base.config, name="overwrite", style_id="lecture")
    unknown_id = "user." + "0" * 32
    bad_id = "user." + "1" * 32
    future = personal.model_dump(mode="json")
    future.update(id=unknown_id, schema_version=99)
    future_path = repo.path(unknown_id)
    future_path.write_text(json.dumps(future), "utf-8")
    repo.path(bad_id).write_bytes(b"invalid-json")
    listing = repo.list()
    assert any(s.id == identity for s in listing.styles) and len(listing.issues) == 2
    before = future_path.read_bytes()
    with pytest.raises(DocumentError):
        repo.save(base.config, name="do not overwrite", style_id=unknown_id)
    assert future_path.read_bytes() == before
    repo.delete(identity)
    assert not repo.path(identity).exists()
    assert base == resolve_style()

def test_font_missing_has_choices_no_automatic_replacement():
    style = resolve_style(overrides={"body": {"font_zh": "MD2WordMissingFont"}})
    report = check_fonts(style, {"宋体", "Arial", "Consolas", "Cambria Math", "Times New Roman", "黑体"})
    assert any(f.name == "MD2WordMissingFont" and f.alternatives for f in report.missing)
    assert any(i.code == "FONT_MISSING" for i in report.issues)
    assert style.config.body.font_zh == "MD2WordMissingFont"
    actual = system_fonts()
    assert actual is None or len(actual) > 0

def test_restyle_existing_ast_and_body_indent_does_not_leak(service, tmp_path, monkeypatch):
    text = "# 标题\n\n正文 123。\n\n3. 列表 300\n4. 列表 400\n\n| a | b |\n|---|---|\n|1|2|\n\n" + chr(96)*3 + "\n    code 789\n" + chr(96)*3 + "\n\n$$x^2$$\n"
    result = service.convert_temp(DocumentSession.create(text).snapshot(), work_root=tmp_path)
    style = resolve_style(overrides={
        "body": {"first_line_chars": 3, "size_pt": 14, "line_spacing": {"mode": "fixed", "points": 24}},
        "page": {"left_mm": 30, "right_mm": 20}})
    # A style change reuses the existing AST. Parsing is deliberately unavailable.
    monkeypatch.setattr(service.parser, "parse", lambda *a, **kw: pytest.fail("unexpected parse"))
    target = result.task_dir / "new-style.docx"
    styled = postprocess(result.docx_path, target, result.checked, style)
    assert styled.validation.passed
    pkg = Package.read(target)
    body = pkg.document.find(q(W, "body"))
    ordinary = next(p for p in body.findall(q(W, "p")) if "正文" in "".join(p.itertext()))
    assert ordinary.find("w:pPr/w:ind", NS).get(q(W, "firstLineChars")) == "300"
    assert ordinary.find("w:pPr/w:spacing", NS).get(q(W, "line")) == "480"
    assert ordinary.find("w:pPr/w:spacing", NS).get(q(W, "lineRule")) == "exact"
    for p in pkg.document.findall(".//w:tbl//w:p", NS):
        assert p.find("w:pPr/w:ind", NS).get(q(W, "firstLineChars")) is None
    for p in body.findall(q(W, "p")):
        style_node = p.find("w:pPr/w:pStyle", NS)
        if p.find("w:pPr/w:numPr", NS) is not None or (
            style_node is not None and style_node.get(q(W, "val")) in {"SourceCode", "MathDisplay"}):
            assert p.find("w:pPr/w:ind", NS).get(q(W, "firstLineChars")) is None
    assert_objects(object_snapshot(Package.read(result.docx_path)), pkg)

@pytest.mark.parametrize("fixture", ["F05_lecture.md", "F18_width_risk.md"])
def test_long_tables_codes_and_risk_warning(service, tmp_path, fixture):
    result = service.convert_temp(read_input(ROOT / "stage4" / fixture, InputMode.MARKDOWN).snapshot(),
                                  style=resolve_style(), work_root=tmp_path)
    assert result.validation.passed
    pkg = Package.read(result.docx_path)
    for table in pkg.document.findall(".//w:tbl", NS):
        width = int(table.find("w:tblPr/w:tblW", NS).get(q(W, "w")))
        grid = [int(x.get(q(W, "w"))) for x in table.findall("w:tblGrid/w:gridCol", NS)]
        assert sum(grid) == width == mm_to_twips(160)
        for row in table.findall(q(W, "tr")):
            assert [int(c.find("w:tcPr/w:tcW", NS).get(q(W, "w"))) for c in row.findall(q(W, "tc"))] == grid
            assert row.find("w:trPr/w:cantSplit", NS).get(q(W, "val")) == "0"
    if fixture.startswith("F18"):
        assert "TABLE_WIDTH_RISK" in {i.code for i in result.checked.issues}
    else:
        assert len(pkg.document.findall(".//w:tr", NS)) >= 70

def test_protection_detects_semantic_numbering_and_math_changes(service, tmp_path):
    result = service.convert_temp(DocumentSession.create("3. test\n\n$x^2$").snapshot(), work_root=tmp_path)
    before = Package.read(result.docx_path)
    protected = object_snapshot(before)
    after = Package.read(result.docx_path)
    after.numbering.find(".//w:numFmt", NS).set(q(W, "val"), "decimal")
    with pytest.raises(DocumentError) as exc:
        assert_objects(protected, after)
    assert exc.value.code == "OBJECT_MISMATCH"

def test_no_page_number_option_and_repeat_formatting(service, tmp_path):
    result = service.convert_temp(DocumentSession.create("正文 123").snapshot(), style=resolve_style(), work_root=tmp_path)
    second = result.task_dir / "again.docx"
    postprocess(result.docx_path, second, result.checked, resolve_style())
    pkg = Package.read(second)
    assert len([n for n in pkg.parts if "md2word-footer" in n]) == 1
    third = result.task_dir / "no-page.docx"
    postprocess(second, third, result.checked, resolve_style(overrides={"page": {"page_numbers": False}}))
    assert not Package.read(third).document.findall(".//w:footerReference", NS)

def test_exclusive_creation_race_never_deletes_someone_elses_file(service, tmp_path, monkeypatch):
    result = service.convert_temp(DocumentSession.create("正文 123").snapshot(), work_root=tmp_path)
    target = result.task_dir / "racing.docx"
    real_open = Path.open
    def racing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "xb":
            with real_open(target, "wb") as stream:
                stream.write(b"another-owner")
            raise FileExistsError("simulated competing creator")
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError):
        postprocess(result.docx_path, target, result.checked, resolve_style())
    assert target.read_bytes() == b"another-owner"

def test_dark_table_header_is_readable_and_footer_reused(service, tmp_path):
    style = resolve_style(overrides={"table":{"header_fill":"17365D"}})
    result = service.convert_temp(DocumentSession.create("| a | b |\n|---|---|\n|1|2|\n").snapshot(),
                                  style=style, work_root=tmp_path)
    package = Package.read(result.docx_path)
    color = package.document.find(".//w:tr/w:tc/w:p/w:r/w:rPr/w:color", NS)
    assert color.get(q(W,"val")) == "FFFFFF"
    off = result.task_dir / "off.docx"
    postprocess(result.docx_path, off, result.checked, resolve_style(overrides={"page":{"page_numbers":False}}))
    on = result.task_dir / "on.docx"
    postprocess(off, on, result.checked, style)
    assert len([n for n in Package.read(on).parts if "md2word-footer" in n]) == 1


