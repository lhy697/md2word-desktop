"""Style-only OOXML transformation with content, relationship and object guards."""
from dataclasses import dataclass
import copy
import posixpath
import re
import time
import unicodedata
import zipfile
from lxml import etree as ET

from ..domain.models import DocumentError, Issue, Severity, digest, failure
from ..domain.style_models import mm_to_twips, pt_to_twips
from ..templates.word_styles import (
    apply_page, apply_styles, child, fonts, format_runs, paragraph_format, paragraph_properties)
from .common import check_budget
from .ooxml import C, M, NS, P, R, W, Package, math_signature, q
from .validation import validate_docx
from .write_plan import Paragraph, all_paragraphs, build_write_plan

@dataclass(frozen=True)
class StyledResult:
    path: object
    validation: object
    issues: tuple[Issue, ...]
    protection_hash: str

def canonical(node):
    return (node.tag, tuple(sorted(node.attrib.items())), node.text or "",
            tuple(canonical(x) for x in node if isinstance(x.tag, str)))

def object_snapshot(package):
    """Exclude only authorized numbering geometry; retain numbering semantics."""
    numbering = copy.deepcopy(package.numbering)
    for node in numbering.findall(".//w:lvl/w:pPr/w:ind", NS):
        node.getparent().remove(node)
    body = package.document.find(q(W, "body"))
    return {
        "math": [math_signature(x) for x in body.findall(".//m:oMath", NS)],
        "numPr": [canonical(x) for x in body.findall(".//w:numPr", NS)],
        "numbering": canonical(numbering),
        "fields": {part: [(x.tag, x.get(q(W, "instr")), x.text or "")
                          for x in root.iter() if x.tag in {q(W, "fldSimple"), q(W, "instrText")}]
                   for part, root in package.parts.items()
                   if part.startswith("word/") and part.endswith(".xml")},
        "relationships": copy.deepcopy(package.relationships),
    }

def assert_objects(before, package):
    after = object_snapshot(package)
    if any(before[key] != after[key] for key in ("math", "numPr", "numbering")):
        raise failure("OBJECT_MISMATCH", "排版修改影响了公式或编号对象。",
                      "停止提交文件并保留原始成品。", "export")
    for part, values in before["fields"].items():
        if after["fields"].get(part) != values:
            raise failure("OBJECT_MISMATCH", "排版修改影响了已有域。",
                          "停止提交文件并保留原始成品。", "export")
    for owner, relationships in before["relationships"].items():
        if any(after["relationships"].get(owner, {}).get(rid) != value for rid, value in relationships.items()):
            raise failure("OBJECT_MISMATCH", "排版修改影响了已有关系。",
                          "停止提交文件并保留原始成品。", "export")
    return digest({k: v for k, v in before.items() if k != "fields"})

def visible_units(text):
    return sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.55 for c in text)

def header_text_color(fill):
    values = [int(fill[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
    luminance = sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))
    return "000000" if luminance > 0.179 else "FFFFFF"

def choose_widths(table, target, total):
    weights = []
    for j in range(target.columns):
        sizes = sorted(visible_units("".join(cell.itertext())) for row in table.findall(q(W, "tr"))
                       for cell in row.findall(q(W, "tc"))[j:j + 1])
        weights.append(max(4, min(28, sizes[min(len(sizes)-1, int(len(sizes)*0.8))] if sizes else 4)))
    minimum = min(total // max(target.columns, 1), mm_to_twips(12))
    remainder = total - minimum * target.columns
    widths = [minimum + int(remainder * weight / sum(weights)) for weight in weights]
    widths[-1] += total - sum(widths)
    return widths

def add_page_field(parts, package, config):
    document = parts["word/document.xml"]
    rels = parts["word/_rels/document.xml.rels"]
    types = parts["[Content_Types].xml"]
    mapping = {x.get("Id"): x for x in rels}
    owned_part = None
    owned_id = None
    for section in document.findall(".//w:sectPr", NS):
        default = next((x for x in section.findall(q(W, "footerReference"))
                        if x.get(q(W, "type")) == "default"), None)
        if default is not None:
            relation = mapping[default.get(q(R, "id"))]
            part = posixpath.normpath("word/" + relation.get("Target"))
            root = parts.get(part)
            if root is None or not any(
                    x.get(q(W, "val")) == "MD2WordPageNumber" for x in root.findall(".//w:pStyle", NS)):
                raise failure("STYLE_FOOTER_CONFLICT", "已有页脚不是当前工具创建的页码。",
                              "从未排版的临时成品重新生成。", "style")
            owned_part, owned_id = part, default.get(q(R, "id"))
            if not config.page.page_numbers:
                section.remove(default)
    if not config.page.page_numbers:
        return
    if owned_part is None:
        # Reuse our unreferenced field after page numbers were toggled off.
        for part, root in parts.items():
            if root.tag == q(W, "ftr") and any(
                    p.get(q(W, "val")) == "MD2WordPageNumber" for p in root.findall(".//w:pStyle", NS)):
                relation = next((r for r in rels if r.get("Type") == R + "/footer"
                                 and posixpath.normpath("word/" + r.get("Target")) == part), None)
                if relation is not None:
                    owned_part, owned_id = part, relation.get("Id")
                    break
    if owned_part is None:
        suffix = 1
        owned_part = "word/md2word-footer.xml"
        while owned_part in parts:
            suffix += 1
            owned_part = f"word/md2word-footer-{suffix}.xml"
        owned_id = "rIdMd2WordFooter"
        while owned_id in mapping:
            owned_id += "X"
        root = ET.Element(q(W, "ftr"), nsmap={"w": W})
        paragraph = ET.SubElement(root, q(W, "p"))
        prop = ET.SubElement(paragraph, q(W, "pPr"))
        child(prop, "pStyle", {"val": "MD2WordPageNumber"})
        field = ET.SubElement(paragraph, q(W, "fldSimple"), {q(W, "instr"): " PAGE "})
        run = ET.SubElement(field, q(W, "r"))
        ET.SubElement(run, q(W, "t")).text = "1"  # Cached field value, not a fixed page count.
        parts[owned_part] = root
        ET.SubElement(rels, q(P, "Relationship"), Id=owned_id,
                      Type=R + "/footer", Target=owned_part.removeprefix("word/"))
        ET.SubElement(types, q(C, "Override"), PartName="/" + owned_part,
                      ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml")
    for section in document.findall(".//w:sectPr", NS):
        if not any(x.get(q(W, "type")) == "default" for x in section.findall(q(W, "footerReference"))):
            section.insert(0, ET.Element(q(W, "footerReference"), {
                q(W, "type"): "default", q(R, "id"): owned_id}))

def postprocess(source, target, checked, style, *, deadline=None, cancel=None, local_formats=()):
    from ..domain.style_models import ResolvedStyle
    if not isinstance(style, ResolvedStyle):
        raise failure("STYLE_INVALID", "排版需要完整、有效的样式快照。",
                      "重新解析样式参数。", "style")
    deadline = deadline if deadline is not None else time.monotonic() + 60
    check_budget(deadline, cancel)
    if target.exists() or source.resolve() == target.resolve():
        raise failure("OUTPUT_CONFLICT", "排版目标已存在，未覆盖。",
                      "使用新的任务文件。", "export")
    original_validation = validate_docx(source, checked, deadline=deadline, cancel=cancel)
    if not original_validation.passed:
        raise DocumentError(original_validation.issues[0])
    package = Package.read(source)
    protected = object_snapshot(package)
    parts = {name: copy.deepcopy(value) for name, value in package.parts.items()}
    document, styles, numbering = parts["word/document.xml"], parts["word/styles.xml"], parts["word/numbering.xml"]
    config = style.config
    apply_styles(styles, config)
    table_header_style = next(s for s in styles.findall(q(W, "style")) if s.get(q(W, "styleId")) == "TableHeader")
    child(child(table_header_style, "rPr"), "color", {"val": header_text_color(config.table.header_fill)})
    for section in document.findall(".//w:sectPr", NS):
        apply_page(section, config.page)
    settings = parts.get("word/settings.xml")
    if settings is not None:
        math_prop = settings.find(q(M, "mathPr"))
        if math_prop is None:
            math_prop = ET.SubElement(settings, q(M, "mathPr"))
        math_font = math_prop.find(q(M, "mathFont"))
        if math_font is None:
            math_font = ET.SubElement(math_prop, q(M, "mathFont"))
        math_font.set(q(M, "val"), config.math.font)
    for level in numbering.findall(".//w:lvl", NS):
        ilvl = int(level.get(q(W, "ilvl")))
        prop = child(level, "pPr")
        child(prop, "ind", {"left": mm_to_twips((ilvl + 1) * config.lists.indent_mm),
                            "hanging": mm_to_twips(config.lists.hanging_mm)})
    issues = []
    plan = build_write_plan(checked)
    def apply_blocks(parent, targets, *, in_table=False, header=False):
        actual = [x for x in parent if x.tag in {q(W, "p"), q(W, "tbl")}]
        if len(actual) != len(targets):
            raise failure("CONTENT_MISMATCH", "排版前结构无法对应。",
                          "停止排版并重新写出。", "export")
        for node, expected in zip(actual, targets):
            check_budget(deadline, cancel)
            if isinstance(expected, Paragraph):
                prop = paragraph_properties(node)
                if expected.heading:
                    fmt = config.headings[expected.heading - 1]
                    paragraph_format(prop, fmt)
                    child(prop, "keepNext", {"val": int(fmt.keep_next)})
                    child(prop, "keepLines", {"val": 1})
                    child(prop, "pageBreakBefore", {"val": int(fmt.page_break_before)})
                    child(prop, "outlineLvl", {"val": fmt.level - 1})
                    format_runs(node, fmt)
                    for run in node.findall(".//w:r", NS):
                        if not any(a.tag == q(M, "oMath") for a in run.iterancestors()):
                            child(child(run, "rPr"), "color", {"val": "000000"})
                elif expected.kind in {"code", "math-source-display"}:
                    fmt = config.code
                    paragraph_format(prop, fmt)
                    child(prop, "wordWrap", {"val": 0})
                    child(prop, "shd", {"val": "clear", "fill": fmt.fill})
                    format_runs(node, fmt, code=True)
                    if expected.kind == "math-source-display":
                        child(prop, "jc", {"val": "center"})
                elif expected.kind == "display":
                    paragraph_format(prop, config.body)
                    child(prop, "pStyle", {"val": "MathDisplay"})
                    child(prop, "jc", {"val": "center"})
                    child(prop, "spacing", {"before": pt_to_twips(config.math.before_pt),
                                           "after": pt_to_twips(config.math.after_pt)})
                elif in_table:
                    paragraph_format(prop, config.table)
                    child(prop, "pStyle", {"val": "TableHeader" if header else "TableBody"})
                    child(prop, "wordWrap", {"val": 0})
                    format_runs(node, config.table, bold=header)
                    if header:
                        for run in node.findall(".//w:r", NS):
                            if not any(a.tag == q(M, "oMath") for a in run.iterancestors()):
                                color = child(child(run, "rPr"), "color", {"val": header_text_color(config.table.header_fill)})
                                for attr in ("themeColor", "themeTint", "themeShade"):
                                    color.attrib.pop(q(W, attr), None)
                elif expected.list_context:
                    paragraph_format(prop, config.body)
                    format_runs(node, config.body)
                elif expected.quote:
                    paragraph_format(prop, config.quote, left_mm=config.quote.indent_mm)
                    format_runs(node, config.quote)
                else:
                    paragraph_format(prop, config.body, first_chars=config.body.first_line_chars)
                    format_runs(node, config.body)
                if expected.list_context and not in_table:
                    indent = child(prop, "ind", {"left": mm_to_twips(
                        (expected.list_context.level + 1) * config.lists.indent_mm)})
                    for attr in ("leftChars", "firstLine", "firstLineChars", "hanging", "hangingChars"):
                        indent.attrib.pop(q(W, attr), None)
                    if expected.numbering:
                        indent.set(q(W, "hanging"), str(mm_to_twips(config.lists.hanging_mm)))
                    else:
                        indent.set(q(W, "firstLine"), "0")
            else:
                width = mm_to_twips(config.page.width_mm - config.page.left_mm - config.page.right_mm)
                widths = choose_widths(node, expected, width)
                prop = child(node, "tblPr")
                child(prop, "tblW", {"type": "dxa", "w": width})
                child(prop, "tblLayout", {"type": "fixed"})
                borders = child(prop, "tblBorders")
                for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
                    child(borders, edge, {"val": "single", "sz": 4, "color": config.table.border_color})
                grid = node.find(q(W, "tblGrid"))
                for col, value in zip(grid, widths):
                    col.set(q(W, "w"), str(value))
                risk = False
                for i, (row, cells) in enumerate(zip(node.findall(q(W, "tr")), expected.rows)):
                    row_prop = child(row, "trPr")
                    child(row_prop, "cantSplit", {"val": 0})
                    if i < expected.header_rows:
                        child(row_prop, "tblHeader", {"val": 1})
                    for j, (cell, blocks) in enumerate(zip(row.findall(q(W, "tc")), cells)):
                        cell_prop = child(cell, "tcPr")
                        child(cell_prop, "tcW", {"type": "dxa", "w": widths[j]})
                        child(cell_prop, "vAlign", {"val": "center"})
                        margin = child(cell_prop, "tcMar")
                        for edge in ("top", "bottom"):
                            child(margin, edge, {"type": "dxa", "w": mm_to_twips(config.table.cell_vertical_mm)})
                        for edge in ("left", "right"):
                            child(margin, edge, {"type": "dxa", "w": mm_to_twips(config.table.cell_horizontal_mm)})
                        if i < expected.header_rows:
                            child(cell_prop, "shd", {"val": "clear", "fill": config.table.header_fill})
                        else:
                            fill = "F6F8FA" if header_text_color(config.table.header_fill) == "FFFFFF" and i % 2 == 0 else "FFFFFF"
                            child(cell_prop, "shd", {"val": "clear", "fill": fill})
                        text = "".join(cell.itertext())
                        available = widths[j] / 20 - config.table.cell_horizontal_mm * 72 / 25.4 * 2
                        tokens = re.findall(r"\S+", text)
                        risk |= any(visible_units(t) * config.table.size_pt > available for t in tokens)
                        risk |= any(len(a[1]) > 100 for block in blocks if isinstance(block, Paragraph)
                                    for a in block.atoms if a[0] == "link_start")
                        risk |= any(len(m.source) > 100 for m in checked.math_records
                                    if m.node_id.startswith(expected.node_id + "/"))
                        apply_blocks(cell, blocks, in_table=True, header=i < expected.header_rows)
                if risk:
                    location = next((x for x in checked.normalized.source_map if x.node_id == expected.node_id), None)
                    issues.append(Issue("TABLE_WIDTH_RISK", Severity.WARNING, "style",
                        "表格存在长串、链接或公式，实际换行可能影响版面。",
                        "在 Word 中检查；内容未删减或截断。", expected.node_id, location))
    apply_blocks(document.find(q(W, "body")), plan.blocks)
    if local_formats:
        from .local_formatting import visit_local
        visit_local(document,checked,local_formats,apply=True,check_budget=lambda:check_budget(deadline,cancel))
    add_page_field(parts, package, config)
    created = False
    try:
        stream = target.open("xb")
        created = True
        with stream, zipfile.ZipFile(source) as old, zipfile.ZipFile(stream, "w") as new:
            names = set()
            for info in old.infolist():
                check_budget(deadline, cancel)
                names.add(info.filename)
                data = ET.tostring(parts[info.filename], xml_declaration=True, encoding="UTF-8") if info.filename in parts else old.read(info.filename)
                new.writestr(info, data)
            for name in parts.keys() - names:
                new.writestr(name, ET.tostring(parts[name], xml_declaration=True, encoding="UTF-8"))
        result = Package.read(target)
        protection_hash = assert_objects(protected, result)
        validation = validate_docx(target, checked, deadline=deadline, cancel=cancel)
        if not validation.passed:
            raise DocumentError(validation.issues[0])
        if local_formats:
            visit_local(result.document,checked,local_formats,apply=False,check_budget=lambda:check_budget(deadline,cancel))
        return StyledResult(target, validation, tuple(issues), protection_hash)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise

