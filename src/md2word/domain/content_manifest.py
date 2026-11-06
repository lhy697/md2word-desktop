"""Ordered structural content, with explicit list items and table row/cell entries."""
from .ast_index import ENUMS, WRAPPERS, inline_text, node_id
from .models import ContentManifest, ManifestEntry, stable_json

def table_rows(content):
    """Yield (row path suffix, row, area) in document order."""
    for i, row in enumerate(content[3][1]):
        yield (3, 1, i), row, "head"
    for b, body in enumerate(content[4]):
        for part in (2, 3):
            for i, row in enumerate(body[part]):
                yield (4, b, part, i), row, "body_head" if part == 2 else "body"
    for i, row in enumerate(content[5][1]):
        yield (5, 1, i), row, "foot"

def block_text(blocks):
    pieces = []
    for block in blocks:
        t, c = block.get("t"), block.get("c")
        if t in {"Plain", "Para"}:
            pieces.append(inline_text(c))
        elif t == "Header":
            pieces.append(inline_text(c[2]))
        elif t in {"CodeBlock", "RawBlock"}:
            pieces.append(c[1])
    return "\n".join(pieces)

def build_manifest(ast: dict) -> ContentManifest:
    entries = []
    def add(identity, kind, parent, payload):
        entries.append(ManifestEntry(identity, kind, parent, len(entries) + 1,
                                     stable_json(payload)))
    def visit(value, path=(), parent=None):
        if isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, path + (i,), parent)
            return
        if not isinstance(value, dict):
            return
        tag = value.get("t")
        if not tag or tag in ENUMS:
            for key, item in value.items():
                visit(item, path + (key,), parent)
            return
        identity, c = node_id(path), value.get("c")
        payload = {}
        if tag == "Str":
            payload = {"text": c}
        elif tag in {"Plain", "Para"}:
            payload = {"text": inline_text(c)}
        elif tag == "Header":
            payload = {"level": c[0], "attributes": c[1], "text": inline_text(c[2])}
        elif tag in {"Code", "CodeBlock"}:
            payload = {"text": c[1], "logical_lines": c[1].split("\n"), "attributes": c[0]}
        elif tag in {"RawInline", "RawBlock"}:
            payload = {"format": c[0], "source": c[1]}
        elif tag == "Math":
            payload = {"type": c[0]["t"], "source": c[1], "status": "unchecked"}
        elif tag in {"Image", "Link"}:
            payload = {"text": inline_text(c[1]), "target": c[2][0],
                       "title": c[2][1], "attributes": c[0]}
        elif tag == "OrderedList":
            payload = {"start": c[0][0], "style": c[0][1]["t"], "delimiter": c[0][2]["t"]}
        elif tag == "Table":
            payload = {"columns": len(c[2]), "column_specs": c[2], "attributes": c[0],
                       "caption": c[1]}
        elif tag not in WRAPPERS | {"BulletList", "BlockQuote", "Space", "SoftBreak",
                                    "LineBreak", "HorizontalRule", "Quoted"}:
            payload = {"preserved_json": value}
        add(identity, tag, parent, payload)
        if tag in {"OrderedList", "BulletList"}:
            items = c[1] if tag == "OrderedList" else c
            prefix = path + ("c", 1) if tag == "OrderedList" else path + ("c",)
            for i, item in enumerate(items):
                item_id = identity + "/item/" + str(i)
                add(item_id, "ListItem", identity, {"index": i})
                visit(item, prefix + (i,), item_id)
            return
        if tag == "Table":
            visit(c[1], path + ("c", 1), identity)
            for row_number, (suffix, row, area) in enumerate(table_rows(c)):
                row_path = path + ("c",) + suffix
                row_id = node_id(row_path) + "/row"
                add(row_id, "TableRow", identity, {"row": row_number, "area": area})
                for j, cell in enumerate(row[1]):
                    cell_path = row_path + (1, j)
                    cell_id = node_id(cell_path) + "/cell"
                    add(cell_id, "TableCell", row_id, {
                        "column": j, "rowspan": cell[2], "colspan": cell[3],
                        "alignment": cell[1]["t"], "text": block_text(cell[4])})
                    visit(cell[4], cell_path + (4,), cell_id)
            return
        if "c" in value:
            visit(c, path + ("c",), identity)
    if ast["meta"]:
        add("n/meta", "Metadata", None, {"preserved_json": ast["meta"]})
    visit(ast["blocks"], ("blocks",))
    return ContentManifest(tuple(entries))
