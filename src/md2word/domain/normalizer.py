"""Explicit visible fallbacks and independently replayable content-change audit."""
import json
from urllib.parse import urlsplit

from .ast_index import (ENUMS, at_path, index_ast, inline_text, node_id, validate_ast)
from .content_manifest import build_manifest, table_rows
from .models import (DocumentError, FrozenAst, Issue, ManifestAudit, NodeOrigin,
                     NormalizedDocument, ParsedDocument, Severity, Transformation,
                     failure, stable_json)

SUPPORTED = {
    "Str", "Space", "SoftBreak", "LineBreak", "Emph", "Strong", "Strikeout",
    "Code", "Math", "Link", "Image", "RawInline", "RawBlock", "Plain", "Para",
    "Header", "BlockQuote", "OrderedList", "BulletList", "CodeBlock",
    "HorizontalRule", "Table", "Quoted",
}
POLICY_VERSION = "support-v2"

def link_allowed(target: str) -> bool:
    if target.startswith("#"):
        return True
    try:
        parsed = urlsplit(target)
        if parsed.scheme.lower() in {"http", "https"}:
            return bool(parsed.netloc) and not any(ord(c) < 32 for c in target)
        return parsed.scheme.lower() == "mailto" and bool(parsed.path)
    except ValueError:
        return False

def rich_description(nodes):
    # A parent fallback consumes its subtree: preserve every nested reference,
    # including images inside formatting, rather than flattening to labels.
    pieces = []
    for n in nodes:
        if n["t"] in {"Link", "Image"}:
            pieces.append(reference_description(n))
        elif n["t"] in {"Emph", "Strong", "Strikeout"}:
            pieces.append(rich_description(n["c"]))
        elif n["t"] == "Quoted":
            # Keep the same literal quote treatment as inline_text.
            pieces.append(rich_description(n["c"][1]))
        else:
            pieces.append(inline_text([n]))
    return "".join(pieces)


def reference_description(node):
    c = node["c"]
    label = rich_description(c[1])
    text = ("[图片未嵌入] 描述：" + label + "；引用：" + c[2][0]
            if node["t"] == "Image" else label + " (" + c[2][0] + ")")
    if c[2][1]:
        text += "；标题：" + c[2][1]
    if c[0] != ["", [], []]:
        text += "；属性：" + stable_json(c[0])
    return text

def has_unknown_descendant(node) -> bool:
    if isinstance(node, list):
        return any(has_unknown_descendant(x) for x in node)
    if not isinstance(node, dict):
        return False
    if "t" in node and node["t"] not in SUPPORTED | ENUMS:
        return True
    return any(has_unknown_descendant(x) for x in node.values())

def fallback_for(node):
    t, c = node["t"], node.get("c")
    attr = ["", [], []]
    if t == "Image" and not has_unknown_descendant(node):
        return "image_visible_v2", {"t": "Code", "c": [attr, reference_description(node)]}
    if t == "RawInline":
        return "raw_inline_visible_v1", {"t": "Code", "c": [attr, c[1]]}
    if t == "RawBlock":
        return "raw_block_visible_v1", {"t": "CodeBlock", "c": [["", ["text"], []], c[1]]}
    if t == "Link" and not link_allowed(c[2][0]) and not has_unknown_descendant(node):
        return "link_visible_v2", {"t": "Code", "c": [attr, reference_description(node)]}
    return None

def audit_normalization(parsed: ParsedDocument, normalized: dict,
                        records: tuple[Transformation, ...], manifest=None) -> ManifestAudit:
    """Only declared, policy-approved replacements may differ, at any granularity."""
    source = parsed.ast.to_dict()
    source_manifest = build_manifest(source)
    if source_manifest != parsed.manifest:
        raise failure("CONTENT_MISMATCH", "原始结构与内容清单不一致。",
                      "停止导出并重新解析。", "normalize")
    seen, prefixes = set(), set()
    for record in records:
        path = record.path
        if (not path or path in prefixes or any(path[:i] in seen for i in range(1, len(path) + 1))
                or record.origin_node_id != node_id(path) or record.target_node_id != node_id(path)):
            raise failure("CONTENT_MISMATCH", "变换记录存在重叠或来源错误。",
                          "停止导出并重新解析。", "normalize")
        try:
            original = at_path(source, path)
            expected = fallback_for(original)
            if (stable_json(original) != record.before_json or expected is None
                    or expected[0] != record.rule or stable_json(expected[1]) != record.after_json):
                raise ValueError("Unapproved replacement")
            at_path(source, path[:-1])[path[-1]] = json.loads(record.after_json)
        except (KeyError, IndexError, TypeError, ValueError):
            raise failure("CONTENT_MISMATCH", "变换不能与原始内容逐项对应。",
                          "停止导出并保留原文。", "normalize") from None
        seen.add(path)
        prefixes.update(path[:i] for i in range(1, len(path) + 1))
    expected_manifest = build_manifest(source)
    if (stable_json(source) != stable_json(normalized)
            or expected_manifest != (manifest or build_manifest(normalized))):
        raise failure("CONTENT_MISMATCH", "规范化产生了未声明的内容变化。",
                      "停止导出并保留原文。", "normalize")
    return ManifestAudit(True, len(source_manifest.entries),
                         len(expected_manifest.entries), len(records))

def normalize(parsed: ParsedDocument | NormalizedDocument, *, deadline=None, cancel=None) -> NormalizedDocument:
    from ..conversion.common import check_budget
    def checkpoint():
        if deadline is not None or cancel is not None:
            check_budget(deadline if deadline is not None else float('inf'), cancel)
    checkpoint()
    if isinstance(parsed, NormalizedDocument):
        if parsed.policy_version != POLICY_VERSION:
            raise failure("NORMALIZATION_VERSION", "规范化策略已改变。",
                          "从原始解析结果重新检查。", "normalize")
        return parsed
    ast = parsed.ast.to_dict()
    validate_ast(ast, parsed.reader_profile.ast_api_version)
    records, issues = [], list(parsed.issues)
    original_locations = {x.node_id: x for x in parsed.source_map}
    def issue(code, severity, message, action, identity):
        issues.append(Issue(code, severity, "normalize", message, action,
                            identity, original_locations.get(identity)))
    if ast["meta"]:
        issue("UNSUPPORTED_NODE", Severity.BLOCKING, "当前不支持隐藏元数据。",
              "将需要保留的内容写入可见正文。", "n/meta")

    visited = 0
    def visit(value, path=()):
        nonlocal visited
        visited += 1
        if visited % 256 == 0:
            checkpoint()
        if isinstance(value, list):
            for i, item in enumerate(value):
                value[i] = visit(item, path + (i,))
            return value
        if not isinstance(value, dict):
            return value
        tag = value.get("t")
        if tag and tag not in ENUMS:
            identity = node_id(path)
            fallback = fallback_for(value)
            if fallback:
                rule, replacement = fallback
                records.append(Transformation(identity, identity, path, rule,
                                               stable_json(value), stable_json(replacement)))
                code = ("IMAGE_NOT_SUPPORTED" if tag == "Image" else
                        "LINK_UNSAFE" if tag == "Link" else "RAW_CONTENT_VISIBLE")
                issue(code, Severity.WARNING, {
                    "Image": "图片未读取，描述和引用已转为可见文本。",
                    "Link": "链接协议未获允许，标签和地址已保留为文本。",
                }.get(tag, "原始标记已转为可见源码，不会执行或透传。"),
                    "导出后检查保留的文本。", identity)
                for child in index_ast(value):
                    if child.path and child.node_type == "Image":
                        issue("IMAGE_NOT_SUPPORTED", Severity.WARNING,
                              "图片未读取，描述和引用已转为可见文本。",
                              "导出后检查保留的文本。", node_id(path + child.path))
                return replacement
            if tag not in SUPPORTED or tag == "Image":
                issue("UNSUPPORTED_NODE", Severity.BLOCKING,
                      "发现尚不能保证保留的文档节点。",
                      "查看片段并将该内容改为普通文本后重试。", identity)
            if tag == "Header" and value["c"][0] > 3:
                issue("HEADING_DEEP", Severity.INFO,
                      "保留了四至六级标题，默认模板重点优化前三层。",
                      "需要时调整标题层级。", identity)
            if tag == "Table":
                c = value["c"]
                complex_table = bool(c[1][0] or c[1][1])
                for _, row, _ in table_rows(c):
                    complex_table |= len(row[1]) != len(c[2])
                    complex_table |= any(
                        cell[2:4] != [1, 1] or any(b.get("t") not in {"Plain", "Para"} for b in cell[4])
                        for cell in row[1])
                if complex_table:
                    issue("UNSUPPORTED_TABLE", Severity.BLOCKING,
                          "当前只验收无合并单元格的简单管道表格。",
                          "保留原文，将复杂表格拆分后重试。", identity)
        for key, item in list(value.items()):
            if key != "t":
                value[key] = visit(item, path + (key,))
        return value
    ast = visit(ast)
    checkpoint()
    validate_ast(ast, parsed.reader_profile.ast_api_version)
    manifest = build_manifest(ast)
    try:
        audit = audit_normalization(parsed, ast, tuple(records), manifest)
    except DocumentError as exc:
        issues.append(exc.issue)
        audit = ManifestAudit(False, len(parsed.manifest.entries), len(manifest.entries), len(records))
    index = index_ast(ast)
    records_by_path = {r.path: r for r in records}
    origins = []
    for info in parsed.node_index:
        if len(origins) % 256 == 0:
            checkpoint()
        replacement = next((records_by_path[info.path[:i]] for i in range(len(info.path), 0, -1)
                            if info.path[:i] in records_by_path), None)
        origins.append(NodeOrigin(
            info.node_id, replacement.target_node_id if replacement else info.node_id,
            "fallback" if replacement else "preserved"))
    source_map = []
    for info in index:
        if len(source_map) % 256 == 0:
            checkpoint()
        if info.node_id in original_locations:
            source_map.append(original_locations[info.node_id])
        else:
            # A future rule that expands nodes must supply explicit new-node origins.
            raise failure("CONTENT_MISMATCH", "新节点缺少来源记录。",
                          "停止导出并保留原文。", "normalize")
    return NormalizedDocument(parsed, FrozenAst.capture(ast), index, tuple(source_map),
                              tuple(origins), tuple(records), parsed.manifest, manifest,
                              audit, tuple(issues), POLICY_VERSION)

