from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import threading
import time

import pytest

from md2word.domain.ast_index import at_path, index_ast, validate_ast
from md2word.domain.content_manifest import build_manifest
from md2word.domain.models import (
    Confidence, DocumentError, DocumentSession, FrozenAst, InputMode,
    ReaderProfile, SourceSpan, stable_json)
from md2word.domain.normalizer import audit_normalization, normalize
from md2word.domain.repairs import (apply_repair, suggest_outer_fence, undo_repair)
from md2word.domain.source_mapping import from_qt_utf16, to_qt_utf16
from md2word.input import MAX_INPUT_BYTES, check_text, read_input
from md2word.parsing import Parser, load_reader_profile

FIXTURES = Path(__file__).parent / "fixtures/stage2"
CASES = json.loads((FIXTURES / "index.json").read_text("utf-8"))

@pytest.fixture(scope="module")
def parser():
    return Parser()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_real_fixtures(parser, case):
    session = read_input(FIXTURES / case["file"], InputMode(case["mode"]))
    before = session.snapshot()
    parsed = parser.parse(before)
    normalized = normalize(parsed)
    assert parsed.snapshot == before
    assert session.current_text == session.original_text == before.text
    assert normalized.audit.passed and normalized.exportable
    assert {x.code for x in normalized.issues} == set(case["issues"])
    assert normalize(normalized) is normalized
    manifest = normalized.manifest
    if "heading_levels" in case:
        assert [e.payload["level"] for e in manifest.of_kind("Header")] == case["heading_levels"]
    if "ordered_starts" in case:
        assert [e.payload["start"] for e in manifest.of_kind("OrderedList")] == case["ordered_starts"]
        assert len(manifest.of_kind("ListItem")) == 6
    if "table_cells" in case:
        assert [e.payload["text"] for e in manifest.of_kind("TableCell")] == case["table_cells"]
        cells = manifest.of_kind("TableCell")
        assert len({e.parent_id for e in cells}) == 4
    if "math_sources" in case:
        assert [e.payload["source"] for e in manifest.of_kind("Math")] == case["math_sources"]
    if "code_texts" in case:
        assert [e.payload["text"] for e in manifest.of_kind("CodeBlock")] == case["code_texts"]
        assert not manifest.of_kind("Math")  # code dollar signs are never math
    if "links" in case:
        assert [{k: e.payload[k] for k in ("text", "target", "title")}
                for e in manifest.of_kind("Link")] == case["links"]
        assert manifest.of_kind("BlockQuote") and manifest.of_kind("HorizontalRule")
    if "fallback_contains" in case:
        result = normalized.ast.to_dict()
        assert not any(i.node_type in {"Image", "RawInline", "RawBlock"} for i in normalized.node_index)
        assert not manifest.of_kind("Link")
        literal = "\n".join(e.payload["text"] for e in manifest.entries
                            if e.kind in {"Code", "CodeBlock", "Str"})
        for value in case["fallback_contains"]:
            assert value in literal
        for record in normalized.fallback_records:
            assert record.origin_node_id in {x.origin_node_id for x in normalized.origins}
        assert all(o.target_node_id in {n.node_id for n in normalized.node_index} for o in normalized.origins)
        # Fallback is plain literal content. Even the HTML writer cannot emit a script.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            html = parser.runner.run(["--from=json", "--to=html"], cwd=Path(tmp),
                                     stdin=stable_json(result).encode("utf-8")).stdout.decode("utf-8")
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert "<img" not in html and "javascript:alert" in html
    if case.get("repair"):
        suggestion = suggest_outer_fence(before).suggestion
        assert suggestion is not None
        repaired = apply_repair(session, suggestion)
        repaired_doc = parser.parse(repaired.snapshot())
        assert repaired_doc.manifest.of_kind("Header")
        assert session.original_text == repaired.original_text == before.text
        restored = undo_repair(repaired, suggestion)
        assert restored.current_text == before.text
    if case["mode"] == "plain":
        assert not any(e.kind in {"Header", "Math", "Strong", "Link", "CodeBlock"} for e in manifest.entries)
        atoms = [e.payload["text"] for e in manifest.of_kind("Str")]
        assert atoms == [line for line in before.text.splitlines() if line]
        assert any(x.startswith("    ") and "\t" in x for x in atoms)
    # A detached work copy cannot mutate either cached AST.
    detached = normalized.ast.to_dict()
    detached["blocks"].clear()
    assert parsed.ast.to_dict()["blocks"] and normalized.ast.to_dict()["blocks"]
    assert build_manifest(parsed.ast.to_dict()) == parsed.manifest

def test_session_snapshot_and_repeatability(parser):
    session = DocumentSession.create()
    initial = session.snapshot()
    changed = session.edit("第一次编辑 😀 123").style_changed()
    assert changed.original_text == initial.text == ""
    assert changed.content_revision == 1 and changed.style_revision == 1
    assert initial.text == ""
    with pytest.raises(FrozenInstanceError):
        initial.text = "bad"
    doc1 = parser.parse(changed.snapshot())
    doc2 = parser.parse(changed.style_changed().snapshot())
    assert doc1.ast == doc2.ast and doc1.manifest == doc2.manifest
    assert doc1.input_key == doc2.input_key
    assert changed.set_mode(InputMode.PLAIN).snapshot().content_hash != changed.snapshot().content_hash
    assert doc1.node_index == doc2.node_index

def test_strict_input_no_newline_rewrite_or_mode_guessing(tmp_path, parser):
    path = tmp_path / "中文 文件.txt"
    raw = b"\xef\xbb\xbf" + "# 中文 😀\r\n\r\n$not_math$\r\n".encode("utf-8")
    path.write_bytes(raw)
    session = read_input(path, InputMode.MARKDOWN)
    assert "\r\n" in session.current_text and not session.current_text.startswith("\ufeff")
    assert path.read_bytes() == raw
    assert parser.parse(session.snapshot()).manifest.of_kind("Header")
    plain = read_input(path, InputMode.PLAIN)
    assert not parser.parse(plain.snapshot()).manifest.of_kind("Header")
    path.write_bytes(b"\xff\xfe\x00secret")
    with pytest.raises(DocumentError) as exc:
        read_input(path, InputMode.MARKDOWN)
    assert exc.value.code == "INPUT_ENCODING"
    assert path.read_bytes() == b"\xff\xfe\x00secret"
    with pytest.raises(DocumentError) as exc:
        read_input(tmp_path / "file.html", InputMode.PLAIN)
    assert exc.value.code == "INPUT_TYPE"

def test_plain_and_markdown_newline_contracts(parser):
    text = "A\r\nB\r\n\r\nC  \r\nD\r\n"
    markdown = parser.parse(DocumentSession.create(text).snapshot())
    assert len(markdown.manifest.of_kind("SoftBreak")) == 1
    assert len(markdown.manifest.of_kind("LineBreak")) == 1
    plain = parser.parse(DocumentSession.create(text, InputMode.PLAIN).snapshot())
    assert len(plain.manifest.of_kind("Para")) == 2
    assert not plain.manifest.of_kind("SoftBreak")
    assert len(plain.manifest.of_kind("LineBreak")) == 3
    assert "C  " in [e.payload["text"] for e in plain.manifest.of_kind("Str")]
    assert plain.snapshot.text == text

def test_ui_jump_rejects_old_revision_and_never_guesses_context(parser):
    from md2word.domain.source_mapping import qt_selection
    session = DocumentSession.create("😀 唯一 $x_1$。")
    parsed = parser.parse(session.snapshot())
    info = next(i for i in parsed.node_index if i.node_type == "Math")
    location = parsed.location(info.node_id)
    a, b = qt_selection(session.snapshot(), location)
    editor_text = session.current_text.encode("utf-16-le")
    assert editor_text[a * 2:b * 2].decode("utf-16-le") == "$x_1$"
    with pytest.raises(DocumentError) as exc:
        qt_selection(session.edit("新内容").snapshot(), location)
    assert exc.value.code == "LOCATION_STALE"
    contextual = replace(location, confidence=Confidence.CONTEXTUAL, span=None)
    assert qt_selection(session.snapshot(), contextual) is None

@pytest.mark.parametrize("text, code", [
    ("x" * (MAX_INPUT_BYTES + 1), "INPUT_TOO_LARGE"),
    ("nul\x00 123", "INPUT_CONTROL_CHARACTER"),
    ("vertical\x0b tab", "INPUT_CONTROL_CHARACTER"),
    ("\ud800", "INPUT_ENCODING"),
], ids=["too-large", "nul", "control", "surrogate"])
def test_invalid_input_is_blocked_without_sanitizing(text, code):
    with pytest.raises(DocumentError) as exc:
        check_text(text)
    assert exc.value.code == code
    assert "123" not in str(exc.value)

@pytest.mark.parametrize("mode", [InputMode.MARKDOWN, InputMode.PLAIN])
def test_empty_and_task_errors(parser, mode):
    normalized = normalize(parser.parse(DocumentSession.create(" \r\n\t", mode).snapshot()))
    assert not normalized.exportable
    assert "INPUT_EMPTY" in {x.code for x in normalized.issues}
    snapshot = DocumentSession.create("123", mode).snapshot()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DocumentError) as exc:
        parser.parse(snapshot, cancel=cancel)
    assert exc.value.code == "PARSE_CANCELLED"
    with pytest.raises(DocumentError) as exc:
        parser.parse(snapshot, deadline=time.monotonic() - 1)
    assert exc.value.code == "PARSE_TIMEOUT"

def test_positions_exclude_code_escapes_and_repeated_formulas(parser):
    tick = chr(96)
    text = ("# 定位 😀\r\n\r\n" + tick + "$u$" + tick + " 与转义 \\$u\\$。\r\n\r\n"
            + tick * 3 + "text\r\n$x$\r\n" + tick * 3 + "\r\n\r\n"
            + "重复 $x$ 与 $x$。唯一 $z_7$。\r\n")
    session = DocumentSession.create(text)
    parsed = parser.parse(session.snapshot())
    formulas = [(at_path(parsed.ast.to_dict(), i.path)["c"][1], parsed.location(i.node_id))
                for i in parsed.node_index if i.node_type == "Math"]
    assert [s for s, _ in formulas] == ["x", "x", "z_7"]
    for _, loc in formulas[:2]:
        assert loc.confidence == Confidence.CONTEXTUAL and loc.span is None
    loc = formulas[-1][1]
    assert loc.confidence == Confidence.EXACT
    assert text[loc.span.start:loc.span.end] == "$z_7$"
    assert loc.session_id == session.session_id and loc.revision == 0
    qt_offset = to_qt_utf16(text, loc.span.start)
    assert from_qt_utf16(text, qt_offset) == loc.span.start
    assert qt_offset == len(text[:loc.span.start].replace("\r\n", "\n").encode("utf-16-le")) // 2
    with pytest.raises(ValueError):
        from_qt_utf16("中😀文", 2)  # inside the emoji's surrogate pair

def test_unique_formula_in_code_does_not_steal_real_position(parser):
    tick = chr(96)
    text = tick * 3 + "\n$x$\n# 标题\n" + tick * 3 + "\n\n标题\n====\n\n$x$\n"
    parsed = parser.parse(DocumentSession.create(text).snapshot())
    for info in parsed.node_index:
        if info.node_type == "Math":
            loc = parsed.location(info.node_id)
            assert loc.span.start == text.rfind("$x$")
        if info.node_type == "Header":
            # Setext headings don't have a proven ATX source range.
            assert parsed.location(info.node_id).span is None

def test_plain_duplicate_lines_have_constructed_exact_offsets(parser):
    text = "😀相同\r\n相同\r\n相同"
    parsed = parser.parse(DocumentSession.create(text, InputMode.PLAIN).snapshot())
    atoms = [i for i in parsed.node_index if i.node_type == "Str"]
    assert [parsed.location(i.node_id).span.start for i in atoms] == [0, 5, 9]
    assert all(parsed.location(i.node_id).confidence == Confidence.EXACT for i in atoms)

def test_repair_stale_ambiguous_and_undo_guard():
    fence = chr(96) * 3
    text = "\r\n" + fence + "md\r\n# 标题 123\r\n" + fence + "\r\n\r\n"
    session = DocumentSession.create(text)
    suggestion = suggest_outer_fence(session.snapshot()).suggestion
    assert suggestion and suggestion.before == text
    assert suggestion.after == "\r\n# 标题 123\r\n\r\n"
    repaired = apply_repair(session, suggestion)
    assert repaired.original_text == text
    for other in (session.edit(text + "x"), DocumentSession.create(text), repaired):
        with pytest.raises(DocumentError) as exc:
            apply_repair(other, suggestion)
        assert exc.value.code == "REPAIR_STALE"
    with pytest.raises(DocumentError):
        undo_repair(repaired.edit("其他内容"), suggestion)
    for ambiguous in [
        fence + "md\n# x\n~~~python\nx\n~~~\n" + fence,
        fence + "md\n# x\n~~~",
        fence + "md\n# x\n",
        fence + "python\n# x\n" + fence,
    ]:
        assert suggest_outer_fence(DocumentSession.create(ambiguous).snapshot()).suggestion is None
    assert suggest_outer_fence(DocumentSession.create(text, InputMode.PLAIN).snapshot()).suggestion is None

@pytest.mark.parametrize("damage", ["paragraph_number", "code_indent", "table_empty_cell", "undeclared_fallback"])
def test_content_audit_rejects_actual_mutations(parser, damage):
    tick = chr(96)
    source = ("段落 12345。\n\n| a | b |\n|---|---|\n| 0 | |\n\n"
              + tick * 3 + "\n    print(6789)\n" + tick * 3 + "\n\n![图 2468](missing.png)\n")
    parsed = parser.parse(DocumentSession.create(source).snapshot())
    result = normalize(parsed)
    ast = result.ast.to_dict()
    if damage == "undeclared_fallback":
        records = ()
    else:
        records = result.fallback_records
        if damage == "paragraph_number":
            info = next(i for i in result.node_index if i.node_type == "Str"
                        and "12345" in at_path(ast, i.path)["c"])
            at_path(ast, info.path)["c"] = "段落 12346。"
        elif damage == "code_indent":
            info = next(i for i in result.node_index if i.node_type == "CodeBlock")
            at_path(ast, info.path)["c"][1] = "print(6789)"
        else:
            table = next(i for i in result.node_index if i.node_type == "Table")
            at_path(ast, table.path)["c"][4][0][3][0][1][1][4] = [
                {"t": "Plain", "c": [{"t": "Str", "c": "injected"}]}]
    with pytest.raises(DocumentError) as exc:
        audit_normalization(parsed, ast, records)
    assert exc.value.code == "CONTENT_MISMATCH"
    assert parsed.ast.to_dict() != ast

def test_fault_injection_unknown_node_and_corrupt_ast(monkeypatch, parser):
    snapshot = DocumentSession.create("固定内容 98765").snapshot()
    real = parser.parse(snapshot).ast.to_dict()
    unknown = json.loads(json.dumps(real))
    unknown["blocks"].append({"t": "FutureBlock", "c": {"important": "数字 2468"}})
    with monkeypatch.context() as patch:
        patch.setattr(parser.runner, "parse", lambda *a, **kw: unknown)
        parsed = parser.parse(snapshot)
        normalized = normalize(parsed)
        assert not normalized.exportable
        assert any(i.code == "UNSUPPORTED_NODE" for i in normalized.issues)
        assert normalized.ast.to_dict()["blocks"][-1] == unknown["blocks"][-1]
        with pytest.raises(DocumentError):
            normalized.require_exportable()
    for broken in [
        {"pandoc-api-version": [9], "meta": {}, "blocks": []},
        {**real, "blocks": [{"t": "Para", "c": "not-a-list"}]},
        {**real, "blocks": [{"t": "Header", "c": [1]}]},
        {**real, "blocks": [{"t": "Table", "c": []}]},
        {**real, "blocks": [{"t": "InlineMath"}]},
    ]:
        with monkeypatch.context() as patch:
            patch.setattr(parser.runner, "parse", lambda *a, **kw: broken)
            with pytest.raises(DocumentError) as exc:
                parser.parse(snapshot)
            assert exc.value.code in {"AST_INVALID", "AST_VERSION_UNSUPPORTED"}
            assert "98765" not in str(exc.value)

def test_complex_table_and_unknown_image_alt_are_blocked(monkeypatch, parser):
    snapshot = DocumentSession.create("| a | b |\n|---|---|\n|1|2|\n").snapshot()
    raw = parser.parse(snapshot).ast.to_dict()
    raw["blocks"][0]["c"][3][1][0][1][0][3] = 2
    with monkeypatch.context() as patch:
        patch.setattr(parser.runner, "parse", lambda *a, **kw: raw)
        assert not normalize(parser.parse(snapshot)).exportable
    image = {"pandoc-api-version": [1, 23, 1], "meta": {}, "blocks": [
        {"t": "Para", "c": [{"t": "Image", "c": [
            ["", [], []], [{"t": "FutureInline", "c": "重要 123"}], ["x.png", ""]]}]}]}
    with monkeypatch.context() as patch:
        patch.setattr(parser.runner, "parse", lambda *a, **kw: image)
        normalized = normalize(parser.parse(snapshot))
        assert not normalized.exportable
        assert normalized.ast.to_dict() == image

def test_reader_inconsistent_profile_rejected():
    from md2word.resources import ResourceLocator
    raw = json.loads(ResourceLocator().file("reader-profile.json").read_text("utf-8"))
    raw["effective_extensions"]["smart"] = True
    with pytest.raises(ValueError):
        ReaderProfile.from_dict(raw)

def test_opaque_indented_code_and_raw_html_cannot_steal_formula_location(parser):
    # The real formula's blockquote source has prefixes; the apparent contiguous
    # $$...$$ lives in code. A naive substring search would select the wrong one.
    text = "> $$\n> x\n> $$\n\n    $$\n    x\n    $$\n"
    parsed = parser.parse(DocumentSession.create(text).snapshot())
    math = [i for i in parsed.node_index if i.node_type == "Math"]
    assert len(math) == 1
    assert parsed.location(math[0].node_id).span is None

    raw = "<script>\n$x$\n</script>\n\n实际公式 $x$。\n"
    parsed = parser.parse(DocumentSession.create(raw).snapshot())
    math = [i for i in parsed.node_index if i.node_type == "Math"]
    assert len(math) == 1
    loc = parsed.location(math[0].node_id)
    assert loc.span and loc.span.start == raw.rfind("$x$")

def test_repeated_visible_paragraphs_with_different_markup_are_contextual(parser):
    parsed = parser.parse(DocumentSession.create("重复 123。\n\n**重复 123。**\n").snapshot())
    paras = [i for i in parsed.node_index if i.node_type == "Para"]
    assert len(paras) == 2
    assert all(parsed.location(i.node_id).span is None for i in paras)

def test_math_display_types_keep_distinct_source_ranges(parser):
    text = "行内 $x$。\n\n$$x$$\n"
    parsed = parser.parse(DocumentSession.create(text).snapshot())
    formulas = [i for i in parsed.node_index if i.node_type == "Math"]
    assert [text[parsed.location(i.node_id).span.start:parsed.location(i.node_id).span.end]
            for i in formulas] == ["$x$", "$$x$$"]

def test_snapshot_rejects_invalid_unicode_with_domain_error():
    with pytest.raises(DocumentError) as exc:
        DocumentSession.create("\ud800").snapshot()
    assert exc.value.code == "INPUT_ENCODING"

@pytest.mark.parametrize("stdout", [b"[]", b"null"], ids=["array", "null"])
def test_fault_injection_non_document_json_returns_domain_error(monkeypatch, parser, stdout):
    from md2word.pandoc import RunResult
    with monkeypatch.context() as patch:
        patch.setattr(parser.runner, "_verified_version", parser.profile.engine_version)
        patch.setattr(parser.runner, "run", lambda *a, **kw: RunResult(stdout, b"", 0.0))
        with pytest.raises(DocumentError) as exc:
            parser.parse(DocumentSession.create("保留原文 123").snapshot())
        assert exc.value.code == "AST_INVALID"



