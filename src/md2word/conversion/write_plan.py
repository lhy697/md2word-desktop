"""AST -> explicit Word layout expectations, independent of OOXML extraction."""
import copy
from dataclasses import dataclass
from ..domain.ast_index import node_id
from ..domain.content_manifest import table_rows
from ..domain.conversion_models import MathStatus
from ..domain.models import FrozenAst, failure
from .math_check import audit_checked

@dataclass(frozen=True)
class NumberRule:
    group: str
    level: int
    number: int
    format: str
    label: str

@dataclass(frozen=True)
class ListContext:
    group: str
    level: int

@dataclass(frozen=True)
class Paragraph:
    node_id: str
    atoms: tuple
    kind: str = "body"
    heading: int | None = None
    numbering: NumberRule | None = None
    quote: bool = False
    list_context: ListContext | None = None

@dataclass(frozen=True)
class Table:
    node_id: str
    columns: int
    rows: tuple
    header_rows: int

@dataclass(frozen=True)
class WritePlan:
    ast: FrozenAst
    blocks: tuple

def merge_atoms(atoms):
    merged = []
    for atom in atoms:
        if atom[0] == "text" and not atom[1]:
            continue
        if merged and atom[0] == merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + atom[1])
        else:
            merged.append(atom)
    return tuple(merged)

def all_paragraphs(blocks):
    for block in blocks:
        if isinstance(block, Paragraph):
            yield block
        else:
            for row in block.rows:
                for cell in row:
                    yield from all_paragraphs(cell)

def build_write_plan(checked) -> WritePlan:
    checked.require_ready()
    audit_checked(checked)
    ast = checked.ast.to_dict()
    math = {r.node_id: r for r in checked.math_records}

    def inline(nodes, path):
        atoms = []
        for i, node in enumerate(nodes):
            atoms.extend(inline_one(node, path + (i,)))
        return merge_atoms(atoms)

    def inline_one(node, p):
        t, c = node["t"], node.get("c")
        record = math.get(node_id(p))
        if t == "Str":
            return (("text", c),)
        if t in {"Space", "SoftBreak"}:
            return (("text", " "),)
        if t == "LineBreak":
            return (("text", "\n"),)
        if t == "Code":
            return (("text", c[1]),)
        if t in {"Strong", "Emph", "Strikeout"}:
            return inline(c, p + ("c",))
        if t == "Quoted":
            left, right = ("‘", "’") if c[0]["t"] == "SingleQuote" else ("“", "”")
            return (("text", left), *inline(c[1], p + ("c", 1)), ("text", right))
        if t == "Link":
            return (("link_start", c[2][0], c[2][1]),
                    *inline(c[1], p + ("c", 1)), ("link_end",))
        if t == "Math" and record and record.status == MathStatus.STRUCTURALLY_PASSED:
            return (("math", record.signature, record.math_type),)
        raise failure("CONTENT_UNVERIFIABLE", "存在尚不能逐项验证的行内结构。",
                      "保留原文并简化该结构。", "export")

    def trim_boundary(items):
        while items and items[0][0]["t"] in {"Space", "SoftBreak"}:
            items.pop(0)
        while items and items[-1][0]["t"] in {"Space", "SoftBreak"}:
            items.pop()
        return items

    def blocks(values, path, depth=-1, quote=False, pending=None, list_context=None):
        projected, expected = [], []
        for i, node in enumerate(values):
            t, c = node["t"], node.get("c")
            p = path + (i,)
            identity = node_id(p)
            def take_number():
                return pending.pop(0) if pending else None
            if t in {"Para", "Plain"}:
                # Display nodes become dedicated paragraphs in both writer input
                # and expectation, including failed equations preserved as Code.
                segments, current = [], []
                has_display = False
                for j, item in enumerate(c):
                    r = math.get(node_id(p + ("c", j)))
                    if r and r.math_type == "DisplayMath":
                        has_display = True
                        if current:
                            prefix = trim_boundary(current)
                            if prefix:
                                segments.append((prefix, "body"))
                        segments.append(([(item, j)], "math-source-display"
                                         if r.status == MathStatus.SOURCE_FALLBACK else "display"))
                        current = []
                    else:
                        current.append((item, j))
                if current or not segments:
                    segments.append((trim_boundary(current) if has_display else current, "body"))
                for items, kind in segments:
                    if not items and has_display:
                        continue
                    if not items:
                        number = take_number()
                        if number is not None:
                            raise failure("CONTENT_UNVERIFIABLE", "空列表首段不能可靠写出。",
                                          "为列表项添加普通文字后重试。", "export")
                        expected.append(Paragraph(identity, (), "empty", quote=quote,
                                                  list_context=list_context))
                        continue  # Explicit empty paragraphs are restored as w:p.
                    if kind == "math-source-display":
                        projected.append({"t": "CodeBlock", "c": [
                            ["", ["text"], []], items[0][0]["c"][1]]})
                    else:
                        projected.append({"t": t, "c": [copy.deepcopy(item) for item, _ in items]})
                    atoms = []
                    for item, j in items:
                        atoms.extend(inline_one(item, p + ("c", j)))
                    expected.append(Paragraph(identity, merge_atoms(atoms), kind,
                                              numbering=take_number(), quote=quote,
                                              list_context=list_context))
            elif t == "Header":
                projected.append(copy.deepcopy(node))
                expected.append(Paragraph(identity, inline(c[2], p + ("c", 2)),
                                          heading=c[0], numbering=take_number(), quote=quote,
                                          list_context=list_context))
            elif t == "CodeBlock":
                projected.append(copy.deepcopy(node))
                expected.append(Paragraph(identity, (("text", c[1]),) if c[1] else (),
                                          "code", numbering=take_number(), quote=quote,
                                          list_context=list_context))
            elif t == "HorizontalRule":
                projected.append(copy.deepcopy(node))
                expected.append(Paragraph(identity, (), "rule", numbering=take_number(),
                                          list_context=list_context))
            elif t == "BlockQuote":
                child_ast, child_expected = blocks(c, p + ("c",), depth, True, pending, list_context)
                projected.append({"t": "BlockQuote", "c": child_ast})
                expected.extend(child_expected)
            elif t in {"OrderedList", "BulletList"}:
                ordered = t == "OrderedList"
                items = c[1] if ordered else c
                prefix = p + ("c", 1) if ordered else p + ("c",)
                start = c[0][0] if ordered else 1
                formats = {"Decimal": "decimal", "LowerRoman": "lowerRoman",
                           "UpperRoman": "upperRoman", "LowerAlpha": "lowerLetter",
                           "UpperAlpha": "upperLetter", "DefaultStyle":
                           ["decimal", "lowerLetter", "lowerRoman"][(depth + 1) % 3]}
                fmt = formats.get(c[0][1]["t"]) if ordered else "bullet"
                if fmt is None or depth + 1 > 8:
                    raise failure("CONTENT_UNVERIFIABLE", "当前不能可靠验证该列表格式或层级。",
                                  "简化列表后重试。", "export")
                delimiter = c[0][2]["t"] if ordered else ""
                label = "%" + str(depth + 2) + (")" if delimiter in {"OneParen", "TwoParens"} else ".")
                if delimiter == "TwoParens":
                    label = "(" + label
                projected_items = []
                for j, item in enumerate(items):
                    rule = NumberRule(identity, depth + 1, start + j, fmt, label)
                    item_pending = [rule]
                    new, exp = blocks(item, prefix + (j,), depth + 1, quote, item_pending,
                                      ListContext(identity, depth + 1))
                    if item_pending:
                        raise failure("CONTENT_UNVERIFIABLE", "列表项缺少可核对的首段。",
                                      "在复杂列表内容前添加普通文本段落。", "export")
                    projected_items.append(new)
                    expected.extend(exp)
                projected.append({"t": t, "c": [copy.deepcopy(c[0]), projected_items]
                                  if ordered else projected_items})
            elif t == "Table":
                if pending:
                    raise failure("CONTENT_UNVERIFIABLE", "不能可靠验证以表格起始的列表项。",
                                  "在表格前添加普通列表文字。", "export")
                new_node = copy.deepcopy(node)
                rows = []
                for suffix, row, area in table_rows(c):
                    cells = []
                    for j, cell in enumerate(row[1]):
                        cp = p + ("c",) + suffix + (1, j, 4)
                        new, exp = blocks(cell[4], cp, -1, False)
                        if not exp:
                            exp = [Paragraph(node_id(cp), ())]
                        target = new_node["c"]
                        for part in suffix + (1, j):
                            target = target[part]
                        target[4] = new
                        cells.append(tuple(exp))
                    rows.append(tuple(cells))
                projected.append(new_node)
                expected.append(Table(identity, len(c[2]), tuple(rows), len(c[3][1])))
            else:
                raise failure("CONTENT_UNVERIFIABLE", "该块结构尚不能可靠验证。",
                              "保留原文并简化结构。", "export")
        return projected, expected

    projected, expected = blocks(ast["blocks"], ("blocks",))
    ast["blocks"] = projected
    return WritePlan(FrozenAst.capture(ast), tuple(expected))

