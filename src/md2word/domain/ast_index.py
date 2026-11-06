"""Pandoc 1.23.1 shape validation and sidecar node paths. No AST annotations."""
from .models import NodeInfo, failure

ENUMS = {
    "InlineMath", "DisplayMath", "SingleQuote", "DoubleQuote",
    "AlignDefault", "AlignLeft", "AlignRight", "AlignCenter",
    "ColWidth", "ColWidthDefault", "DefaultStyle", "Example", "Decimal",
    "LowerRoman", "UpperRoman", "LowerAlpha", "UpperAlpha", "DefaultDelim",
    "Period", "OneParen", "TwoParens", "AuthorInText", "SuppressAuthor", "NormalCitation",
}
INLINE = {"Str", "Space", "SoftBreak", "LineBreak", "Emph", "Underline", "Strong",
          "Strikeout", "Superscript", "Subscript", "SmallCaps", "Quoted", "Cite",
          "Code", "Math", "RawInline", "Link", "Image", "Note", "Span"}
BLOCK = {"Plain", "Para", "LineBlock", "CodeBlock", "RawBlock", "BlockQuote",
         "OrderedList", "BulletList", "DefinitionList", "Header", "HorizontalRule",
         "Table", "Div", "Figure", "Null"}
WRAPPERS = {"Emph", "Underline", "Strong", "Strikeout", "Superscript", "Subscript", "SmallCaps"}

def node_id(path: tuple) -> str:
    return "n/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in path)

def at_path(value, path):
    for part in path:
        value = value[part]
    return value

def inline_text(nodes) -> str:
    pieces = []
    for n in nodes:
        t, c = n.get("t"), n.get("c")
        if t in {"Str", "Code", "Math", "RawInline"}:
            pieces.append(c if t == "Str" else c[-1])
        elif t in {"Space", "SoftBreak"}:
            pieces.append(" ")
        elif t == "LineBreak":
            pieces.append("\n")
        elif t in WRAPPERS:
            pieces.append(inline_text(c))
        elif t in {"Link", "Image", "Span", "Quoted", "Cite"}:
            pieces.append(inline_text(c[1]))
    return "".join(pieces)

def index_ast(ast: dict) -> tuple[NodeInfo, ...]:
    result = []
    section = ""
    def visit(value, path=(), parent=None):
        nonlocal section
        if isinstance(value, dict):
            tag = value.get("t")
            if isinstance(tag, str) and tag not in ENUMS:
                current = node_id(path)
                if tag == "Header":
                    section = inline_text(value["c"][2])
                result.append(NodeInfo(current, path, tag, parent, len(result) + 1, section))
                parent = current
            for key, item in value.items():
                if key != "t":
                    visit(item, path + (key,), parent)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, path + (i,), parent)
    visit(ast)
    return tuple(result)

def validate_ast(ast: dict, api: tuple[int, ...], *, max_nodes=100_000, max_depth=96):
    def require(ok):
        if not ok:
            raise failure("AST_INVALID", "解析结果结构不完整或不符合约定。",
                          "保留原文并重新解析；若重复出现，请反馈问题。")
    require(isinstance(ast, dict) and set(ast) == {"pandoc-api-version", "meta", "blocks"})
    if ast["pandoc-api-version"] != list(api):
        raise failure("AST_VERSION_UNSUPPORTED", "文档结构版本与当前引擎不一致。",
                      "使用随程序提供的转换资源。")
    require(isinstance(ast["meta"], dict) and isinstance(ast["blocks"], list))
    count = 0
    stack = [(ast, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > max_depth:
            raise failure("INPUT_COMPLEXITY", "文档嵌套超过当前处理上限。",
                          "拆分过深的嵌套后重试。")
        if isinstance(value, dict):
            if "t" in value:
                count += 1
                if count > max_nodes:
                    raise failure("INPUT_COMPLEXITY", "文档节点超过当前处理上限。",
                                  "请拆分文档，不会截断原文。")
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
        else:
            require(value is None or type(value) in (str, int, float, bool))

    def arr(value, length=None):
        require(isinstance(value, list) and (length is None or len(value) == length))
        return value

    def attr(value):
        arr(value, 3)
        require(isinstance(value[0], str))
        require(isinstance(value[1], list) and all(isinstance(x, str) for x in value[1]))
        arr(value[2])
        for pair in value[2]:
            arr(pair, 2)
            require(all(isinstance(x, str) for x in pair))

    def enum(value, allowed):
        require(isinstance(value, dict) and set(value) == {"t"} and value["t"] in allowed)

    def nodes(value, category):
        arr(value)
        for item in value:
            node(item, category)

    def row(value):
        arr(value, 2)
        attr(value[0])
        arr(value[1])
        for cell in value[1]:
            arr(cell, 5)
            attr(cell[0])
            enum(cell[1], {"AlignDefault", "AlignLeft", "AlignRight", "AlignCenter"})
            require(type(cell[2]) is int and cell[2] > 0
                    and type(cell[3]) is int and cell[3] > 0)
            nodes(cell[4], "block")

    def node(value, category):
        require(isinstance(value, dict) and isinstance(value.get("t"), str))
        require(set(value) <= {"t", "c"})
        t, c = value["t"], value.get("c")
        require(t not in ENUMS)
        if t in INLINE | BLOCK:
            require(t in (INLINE if category == "inline" else BLOCK))
        else:
            return
        if t == "Str":
            require(isinstance(c, str))
        elif t in {"Space", "SoftBreak", "LineBreak", "HorizontalRule", "Null"}:
            require("c" not in value)
        elif t in WRAPPERS or t in {"Plain", "Para", "BlockQuote"}:
            nodes(c, "block" if t == "BlockQuote" else "inline")
        elif t in {"Code", "CodeBlock"}:
            arr(c, 2)
            attr(c[0])
            require(isinstance(c[1], str))
        elif t in {"RawInline", "RawBlock"}:
            arr(c, 2)
            require(all(isinstance(x, str) for x in c))
        elif t == "Math":
            arr(c, 2)
            enum(c[0], {"InlineMath", "DisplayMath"})
            require(isinstance(c[1], str))
        elif t in {"Link", "Image"}:
            arr(c, 3)
            attr(c[0])
            nodes(c[1], "inline")
            arr(c[2], 2)
            require(all(isinstance(x, str) for x in c[2]))
        elif t == "Header":
            arr(c, 3)
            require(type(c[0]) is int and 1 <= c[0] <= 6)
            attr(c[1])
            nodes(c[2], "inline")
        elif t == "Quoted":
            arr(c, 2)
            enum(c[0], {"SingleQuote", "DoubleQuote"})
            nodes(c[1], "inline")
        elif t in {"BulletList", "OrderedList"}:
            items = c
            if t == "OrderedList":
                arr(c, 2)
                arr(c[0], 3)
                require(type(c[0][0]) is int and c[0][0] >= 0)
                enum(c[0][1], {"DefaultStyle", "Example", "Decimal", "LowerRoman",
                              "UpperRoman", "LowerAlpha", "UpperAlpha"})
                enum(c[0][2], {"DefaultDelim", "Period", "OneParen", "TwoParens"})
                items = c[1]
            arr(items)
            for item in items:
                nodes(item, "block")
        elif t == "Table":
            arr(c, 6)
            attr(c[0])
            arr(c[1], 2)
            if c[1][0] is not None:
                nodes(c[1][0], "inline")
            nodes(c[1][1], "block")
            arr(c[2])
            for spec in c[2]:
                arr(spec, 2)
                enum(spec[0], {"AlignDefault", "AlignLeft", "AlignRight", "AlignCenter"})
                require(isinstance(spec[1], dict) and spec[1].get("t") in {"ColWidth", "ColWidthDefault"})
                if spec[1]["t"] == "ColWidth":
                    require(type(spec[1].get("c")) in (float, int) and spec[1]["c"] >= 0)
            for part in (c[3], c[5]):
                arr(part, 2)
                attr(part[0])
                for item in arr(part[1]):
                    row(item)
            for body in arr(c[4]):
                arr(body, 4)
                attr(body[0])
                require(type(body[1]) is int and body[1] >= 0)
                for item in arr(body[2]) + arr(body[3]):
                    row(item)
    nodes(ast["blocks"], "block")
