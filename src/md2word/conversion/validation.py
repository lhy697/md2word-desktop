"""Compare independently extracted Word structure against checked AST expectations."""
import time
from ..domain.conversion_models import ValidationReport
from ..domain.models import DocumentError, Issue, Severity
from .common import check_budget
from .ooxml import M, NS, W, Package, math_signature, q
from .write_plan import Paragraph, Table, build_write_plan, merge_atoms

class Reader:
    def __init__(self, package, locations=()):
        self.package = package
        self.locations = {x.node_id: x for x in locations}
        self.styles = {x.get(q(W, "styleId")): x for x in package.styles.findall(q(W, "style"))}
        self.abstract = {x.get(q(W, "abstractNumId")): x
                         for x in package.numbering.findall(q(W, "abstractNum"))}
        self.nums = {x.get(q(W, "numId")): x for x in package.numbering.findall(q(W, "num"))}
        self.counters = {}
        self.groups = {}
        self.reverse_groups = {}
        self.paragraphs = self.tables = self.equations = self.atoms = 0

    def style_chain(self, paragraph):
        style = paragraph.find("w:pPr/w:pStyle", NS)
        identity = style.get(q(W, "val")) if style is not None else None
        seen = set()
        while identity:
            if identity in seen or identity not in self.styles:
                raise ValueError("Invalid paragraph style")
            seen.add(identity)
            node = self.styles[identity]
            yield identity, node
            base = node.find(q(W, "basedOn"))
            identity = base.get(q(W, "val")) if base is not None else None

    def property(self, paragraph, name):
        value = paragraph.find("w:pPr/w:" + name, NS)
        if value is not None:
            return value
        for _, style in self.style_chain(paragraph):
            value = style.find("w:pPr/w:" + name, NS)
            if value is not None:
                return value
        return None

    def numbering(self, paragraph):
        prop = self.property(paragraph, "numPr")
        if prop is None:
            return None
        num = prop.find(q(W, "numId"))
        level = prop.find(q(W, "ilvl"))
        if num is None:
            raise ValueError("Numbering lacks id")
        num_id = num.get(q(W, "val"))
        if num_id == "0":
            return None
        ilvl = int(level.get(q(W, "val"))) if level is not None else 0
        instance = self.nums[num_id]
        abstract = instance.find(q(W, "abstractNumId"))
        definitions = self.abstract[abstract.get(q(W, "val"))]
        definition = next(x for x in definitions.findall(q(W, "lvl"))
                          if x.get(q(W, "ilvl")) == str(ilvl))
        override = next((x for x in instance.findall(q(W, "lvlOverride"))
                         if x.get(q(W, "ilvl")) == str(ilvl)), None)
        if override is not None and override.find(q(W, "lvl")) is not None:
            definition = override.find(q(W, "lvl"))
        fmt = definition.find(q(W, "numFmt")).get(q(W, "val"))
        label = definition.find(q(W, "lvlText")).get(q(W, "val"))
        if fmt == "bullet" and not label.strip():
            return None  # Pandoc's explicit non-bullet continuation paragraphs.
        start_node = override.find(q(W, "startOverride")) if override is not None else None
        if start_node is None:
            start_node = definition.find(q(W, "start"))
        start = int(start_node.get(q(W, "val"))) if start_node is not None else 1
        key = (num_id, ilvl)
        number = self.counters[key] + 1 if key in self.counters else start
        self.counters[key] = number
        for prior in list(self.counters):
            if prior[0] == num_id and prior[1] > ilvl:
                del self.counters[prior]
        return num_id, ilvl, fmt, label, number

    def paragraph_atoms(self, paragraph):
        atoms = []
        def visit(element, display=False):
            tag = element.tag
            if tag == q(M, "oMath"):
                self.equations += 1
                atoms.append(("math", math_signature(element),
                              "DisplayMath" if display else "InlineMath"))
                return
            if tag == q(M, "oMathPara"):
                for child in element:
                    if child.tag != q(M, "oMathParaPr"):
                        visit(child, True)
                return
            if tag in {q(W, "pPr"), q(W, "rPr"), q(W, "bookmarkStart"),
                       q(W, "bookmarkEnd"), q(W, "proofErr"), q(W, "lastRenderedPageBreak")}:
                return
            if tag == q(W, "hyperlink"):
                target, title = self.package.hyperlink(element)
                atoms.append(("link_start", target, title))
                for child in element:
                    visit(child)
                atoms.append(("link_end",))
                return
            if tag == q(W, "t"):
                atoms.append(("text", element.text or ""))
                return
            if tag in {q(W, "br"), q(W, "cr")}:
                if element.get(q(W, "type"), "textWrapping") != "textWrapping":
                    raise ValueError("Unexpected pagination break")
                atoms.append(("text", "\n"))
                return
            if tag == q(W, "tab"):
                atoms.append(("text", "\t"))
                return
            if tag == q(W, "noBreakHyphen"):
                atoms.append(("text", "\u2011"))
                return
            if tag == q(W, "softHyphen"):
                atoms.append(("text", "\u00ad"))
                return
            if tag == q(W, "pict"):
                rect = element.find("{urn:schemas-microsoft-com:vml}rect")
                if (rect is None or rect.get("{urn:schemas-microsoft-com:office:office}hr") != "t"
                        or len(element) != 1 or len(rect)):
                    raise ValueError("Unsupported picture")
                atoms.append(("rule",))
                return
            if tag not in {q(W, "p"), q(W, "r")}:
                raise ValueError("Unsupported body content")
            for child in element:
                visit(child, display)
        visit(paragraph)
        return merge_atoms(atoms)

    def compare_paragraph(self, actual, expected):
        if actual.tag != q(W, "p"):
            raise ValueError("Expected paragraph")
        self.paragraphs += 1
        atoms = self.paragraph_atoms(actual)
        target = (("rule",),) if expected.kind == "rule" else expected.atoms
        if atoms != target:
            raise ValueError("Paragraph atoms differ")
        self.atoms += len(atoms)
        outline = self.property(actual, "outlineLvl")
        heading = int(outline.get(q(W, "val"))) + 1 if outline is not None else None
        if heading == 10:  # Word's body-text outline level
            heading = None
        if heading != expected.heading:
            raise ValueError("Heading level differs")
        styles = {identity for identity, _ in self.style_chain(actual)}
        if ("SourceCode" in styles) != (expected.kind in {"code", "math-source-display"}):
            raise ValueError("Code paragraph structure differs")
        if expected.quote and expected.kind == "body" and expected.numbering is None and not expected.heading:
            if "BlockText" not in styles:
                raise ValueError("Quote paragraph lost")
        if expected.kind == "math-source-display":
            alignment = self.property(actual, "jc")
            if alignment is None or alignment.get(q(W, "val")) != "center":
                raise ValueError("Display fallback is not a separate centered paragraph")
        number = self.numbering(actual)
        if (number is None) != (expected.numbering is None):
            raise ValueError("Numbering presence differs")
        if number is not None:
            num_id, level, fmt, label, value = number
            rule = expected.numbering
            if level != rule.level or fmt != rule.format:
                raise ValueError("Numbering format/level differs")
            if fmt != "bullet" and (value != rule.number or label != rule.label):
                raise ValueError("Numbering start/order differs")
            if rule.group in self.groups and self.groups[rule.group] != num_id:
                raise ValueError("List numbering unexpectedly changes instance")
            if num_id in self.reverse_groups and self.reverse_groups[num_id] != rule.group:
                raise ValueError("Independent list did not restart")
            self.groups[rule.group] = num_id
            self.reverse_groups[num_id] = rule.group

    def content_children(self, parent):
        values = []
        ignored = {q(W, "sectPr"), q(W, "tcPr"), q(W, "bookmarkStart"),
                   q(W, "bookmarkEnd"), q(W, "proofErr")}
        for child in parent:
            if child.tag in ignored:
                if child.findall(".//w:t", NS) or child.findall(".//m:oMath", NS):
                    raise ValueError("Content hidden in structural properties")
                continue
            if child.tag not in {q(W, "p"), q(W, "tbl")}:
                raise ValueError("Unsupported block content")
            values.append(child)
        return values

    def compare(self, actual, expected, deadline, cancel):
        if len(actual) != len(expected):
            raise ValueError("Block count differs")
        for item, target in zip(actual, expected):
            check_budget(deadline, cancel)
            try:
                if isinstance(target, Paragraph):
                    self.compare_paragraph(item, target)
                else:
                    if item.tag != q(W, "tbl"):
                        raise ValueError("Table missing")
                    self.tables += 1
                    if any(c.tag not in {q(W, "tblPr"), q(W, "tblGrid"), q(W, "tr")} for c in item):
                        raise ValueError("Unexpected table content")
                    if len(item.findall("w:tblGrid/w:gridCol", NS)) != target.columns:
                        raise ValueError("Table grid differs")
                    rows = item.findall(q(W, "tr"))
                    if len(rows) != len(target.rows):
                        raise ValueError("Table row count differs")
                    for i, (row, cells) in enumerate(zip(rows, target.rows)):
                        if any(c.tag not in {q(W, "trPr"), q(W, "tc")} for c in row):
                            raise ValueError("Unexpected row content")
                        actual_cells = row.findall(q(W, "tc"))
                        if len(actual_cells) != len(cells):
                            raise ValueError("Table cell count differs")
                        if (row.find("w:trPr/w:tblHeader", NS) is not None) != (i < target.header_rows):
                            raise ValueError("Table header semantics differ")
                        for cell, blocks in zip(actual_cells, cells):
                            span = cell.find("w:tcPr/w:gridSpan", NS)
                            if ((span is not None and span.get(q(W, "val")) != "1")
                                    or cell.find("w:tcPr/w:vMerge", NS) is not None):
                                raise ValueError("Unexpected merged cell")
                            self.compare(self.content_children(cell), blocks, deadline, cancel)
            except (ValueError, KeyError, StopIteration, TypeError, AttributeError):
                raise DocumentError(Issue("CONTENT_MISMATCH", Severity.BLOCKING, "export",
                    "Word 中的结构或内容与清单不一致。", "停止提交文件并重新检查。",
                    target.node_id, self.locations.get(target.node_id))) from None

def validate_docx(path, checked, *, deadline=None, cancel=None):
    deadline = deadline if deadline is not None else time.monotonic() + 60
    check_budget(deadline, cancel)
    try:
        plan = build_write_plan(checked)
        package = Package.read(path)
        reader = Reader(package, checked.normalized.source_map)
        reader.compare(reader.content_children(package.document.find(q(W, "body"))),
                       plan.blocks, deadline, cancel)
        check_budget(deadline, cancel)
        return ValidationReport(True, (), reader.paragraphs, reader.tables,
                                reader.equations, reader.atoms)
    except DocumentError as exc:
        return ValidationReport(False, (exc.issue,))
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return ValidationReport(False, (Issue("CONTENT_MISMATCH", Severity.BLOCKING, "export",
            "Word 结构或内容无法与清单可靠对应。", "停止提交文件并重新转换。"),))

