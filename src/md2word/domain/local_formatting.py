"""Document-local formatting, anchored to normalized paragraphs, never source markup."""
from dataclasses import dataclass
from bisect import bisect_right
import copy
from typing import Literal

from pydantic import Field, field_validator, model_validator
from .ast_index import node_id
from .models import digest, failure, Issue, Severity
from .style_models import StrictModel, FontStyle, LineSpacing


class CharacterFormat(StrictModel):
    font_zh: str | None = Field(default=None,min_length=1,max_length=80)
    font_latin: str | None = Field(default=None,min_length=1,max_length=80)
    size_pt: float | None = Field(default=None, ge=6, le=48)
    letter_spacing_pt: float | None = Field(default=None, ge=-2, le=20)
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None

    @field_validator("font_zh", "font_latin")
    @classmethod
    def font_name(cls, value):
        return FontStyle.valid_font(value) if value is not None else None

    @field_validator("size_pt")
    @classmethod
    def word_half_points(cls,value):
        if value is not None and value*2 != round(value*2):
            raise ValueError("Word font size uses half points")
        return value

    def empty(self):
        return all(value is None for value in self.model_dump().values())


class ParagraphFormat(StrictModel):
    line_spacing: LineSpacing | None = None
    before_pt: float | None = Field(default=None, ge=0, le=72)
    after_pt: float | None = Field(default=None, ge=0, le=72)
    alignment: Literal["left", "center", "right", "justify"] | None = None

    def empty(self):
        return all(value is None for value in self.model_dump().values())


class LocalRun(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    format: CharacterFormat

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end or self.format.empty():
            raise ValueError("Invalid local run")
        return self


class LocalParagraph(StrictModel):
    node_id: str = Field(pattern=r"^n/[A-Za-z0-9_~/.-]{1,1024}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(max_length=2_000_000)
    index: int = Field(ge=0)
    count: int = Field(ge=1)
    before: str = ""
    after: str = ""
    runs: tuple[LocalRun, ...] = Field(default=(), max_length=512)
    paragraph: ParagraphFormat = Field(default_factory=ParagraphFormat)

    @field_validator("runs", mode="before")
    @classmethod
    def json_runs(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def bounds(self):
        previous = 0
        for run in self.runs:
            if run.start < previous or run.end > len(self.text):
                raise ValueError("Overlapping or out-of-bounds formatting")
            previous = run.end
        return self


def validate_formats(values):
    parsed = tuple(LocalParagraph.model_validate(value) for value in values)
    if (len(parsed) > 128 or len({p.node_id for p in parsed}) != len(parsed)
            or sum(len(p.text) for p in parsed) > 2_000_000
            or sum(len(p.runs) for p in parsed) > 2048):
        raise ValueError("Local formatting budget exceeded")
    return parsed


@dataclass(frozen=True)
class LocalTarget:
    node_id: str
    path: tuple
    kind: str
    text: str
    signature: str
    index: int
    count: int
    before: str
    after: str

    def anchor(self, source_hash, runs=(), paragraph=None):
        return LocalParagraph(node_id=self.node_id, source_hash=source_hash, signature=self.signature,
            text=self.text, index=self.index, count=self.count, before=self.before, after=self.after,
            runs=runs, paragraph=paragraph or ParagraphFormat())


def inline_parts(nodes, path, position=0):
    """Yield leaf/path/normal offset/text; a math object occupies one opaque unit."""
    cursor = position
    for index, node in enumerate(nodes):
        p = path + (index,)
        t, c = node["t"], node.get("c")
        if t == "Str":
            text = c
        elif t in {"Space", "SoftBreak"}:
            text = " "
        elif t == "LineBreak":
            text = "\n"
        elif t == "Math":
            text = "\ufffc"
        elif t == "Code":
            text = c[1]
        elif t in {"Strong", "Emph", "Strikeout", "Underline", "Superscript", "Subscript", "SmallCaps"}:
            parts = list(inline_parts(c, p + ("c",), cursor))
            yield from parts
            cursor += sum(len(item[3]) for item in parts)
            continue
        elif t in {"Link", "Span", "Cite", "Quoted"}:
            if t == "Quoted":
                left, right = ("‘", "’") if c[0]["t"] == "SingleQuote" else ("“", "”")
                yield ({"t":"Str", "c":left}, p + ("left",), cursor, left)
                cursor += 1
            parts = list(inline_parts(c[1], p + ("c", 1), cursor))
            yield from parts
            cursor += sum(len(item[3]) for item in parts)
            if t == "Quoted":
                yield ({"t":"Str", "c":right}, p + ("right",), cursor, right)
                cursor += 1
            continue
        else:
            raise failure("LOCAL_FORMAT_UNSUPPORTED", "该结构尚不能进行局部排版。", "保留原文，选择普通文本后重试。", "style")
        yield node, p, cursor, text
        cursor += len(text)


def targets_for(document):
    ast = document.ast.to_dict()
    blocks = []
    def visit(value, path=()):
        if isinstance(value, dict):
            kind = value.get("t")
            if kind in {"Para", "Plain", "Header", "CodeBlock"}:
                c = value["c"]
                if kind == "CodeBlock":
                    text = c[1]
                else:
                    nodes, suffix = (c[2], ("c", 2)) if kind == "Header" else (c, ("c",))
                    text = "".join(part[3] for part in inline_parts(nodes, path + suffix))
                identity_value = copy.deepcopy(value)
                if kind == "Header":
                    # Pandoc's auto-generated heading ID follows occurrence order,
                    # so it must never make duplicate headings look unambiguous.
                    identity_value["c"][1][0] = ""
                blocks.append((node_id(path),path,kind,text,digest(identity_value)))
                return
            for key, item in value.items():
                if key != "t":
                    visit(item, path + (key,))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, path + (i,))
    visit(ast)
    return tuple(LocalTarget(*item, i, len(blocks), blocks[i-1][4] if i else "",
                             blocks[i+1][4] if i+1 < len(blocks) else "") for i, item in enumerate(blocks))


def merge_runs(existing, start, end, patch=None, *, clear=False):
    cuts = sorted({start, end, *(r.start for r in existing), *(r.end for r in existing)})
    output = []
    for left, right in zip(cuts, cuts[1:]):
        current = next((r.format for r in existing if r.start <= left and right <= r.end), CharacterFormat())
        if start <= left and right <= end:
            if clear:
                current = CharacterFormat()
            elif patch is not None:
                raw = current.model_dump()
                raw.update(patch.model_dump(exclude_none=True))
                current = CharacterFormat.model_validate(raw)
        if current.empty():
            continue
        if output and output[-1].end == left and output[-1].format == current:
            output[-1] = LocalRun(start=output[-1].start, end=right, format=current)
        else:
            output.append(LocalRun(start=left, end=right, format=current))
    return tuple(output)


def resolve_formats(document, values):
    targets = targets_for(document)
    by_id = {t.node_id:t for t in targets}
    by_signature = {}
    for target in targets:
        by_signature.setdefault(target.signature, []).append(target)
    source_hash = document.parsed.snapshot.content_hash
    resolved, unresolved, origins = [], [], {}
    for value in validate_formats(values):
        target = by_id.get(value.node_id)
        runs = value.runs
        if value.source_hash == source_hash and target and target.signature == value.signature:
            pass
        elif len(by_signature.get(value.signature, ())) == 1:
            target = by_signature[value.signature][0]
        elif (target and target.count == value.count and target.index == value.index
              and target.before == value.before and target.after == value.after
              and len(by_signature.get(value.signature, ())) <= 1):
            # Only a structurally isolated paragraph edit is eligible for rebase.
            # Retain formatting on unchanged character ranges; deleted text loses it.
            prefix = 0
            while prefix < min(len(value.text),len(target.text)) and value.text[prefix] == target.text[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < min(len(value.text),len(target.text))-prefix
                   and value.text[len(value.text)-suffix-1] == target.text[len(target.text)-suffix-1]):
                suffix += 1
            mapped = []
            old_middle_end = len(value.text)-suffix
            delta = len(target.text)-len(value.text)
            for run in runs:
                if run.end <= prefix:
                    mapped.append(run)
                elif run.start >= old_middle_end:
                    mapped.append(LocalRun(start=run.start+delta,end=run.end+delta,format=run.format))
                elif run.start <= prefix and run.end >= old_middle_end:
                    # The edit is contained inside one formatted run: inserted
                    # text inherits it. Partially intersected runs are removed,
                    # rather than leaking an ambiguous common letter to a neighbor.
                    if run.start < run.end+delta:
                        mapped.append(LocalRun(start=run.start,end=run.end+delta,format=run.format))
            runs = tuple(mapped)
        else:
            unresolved.append(value)
            continue
        if not runs and value.paragraph.empty():
            continue
        if target.node_id in origins:
            for original in (origins[target.node_id],value):
                if not any(p.node_id == original.node_id for p in unresolved):
                    unresolved.append(original)
            resolved = [p for p in resolved if p.node_id != target.node_id]
        else:
            origins[target.node_id] = value
            resolved.append(target.anchor(source_hash, runs, value.paragraph))
    issues = ()
    if unresolved:
        issues = (Issue("LOCAL_FORMAT_STALE", Severity.BLOCKING, "style",
            "部分局部排版无法准确定位，已保留设置并暂停导出。", "撤销原文修改，或清除失效局部排版后重新设置。"),)
    return targets, tuple(resolved), tuple(unresolved), issues


def apply_selection(document, existing, selection, character=None, paragraph=None, *, clear=False):
    targets, resolved, unresolved, _ = resolve_formats(document, existing)
    by_token = {f"lf{i}":t for i,t in enumerate(targets)}
    values = {p.node_id:p for p in resolved}
    if len(selection) > 64:
        raise ValueError("Select at most 64 paragraphs")
    selected_ids = set()
    for item in selection:
        target = by_token.get(item.get("block"))
        start, end = item.get("start"), item.get("end")
        if (target is None or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(target.text)):
            raise ValueError("Invalid or stale selection")
        old = values.get(target.node_id, target.anchor(document.parsed.snapshot.content_hash))
        selected_ids.add(target.node_id)
        runs = merge_runs(old.runs,start,end,character,clear=clear)
        para = ParagraphFormat() if clear else ParagraphFormat.model_validate({
            **old.paragraph.model_dump(), **(paragraph.model_dump(exclude_none=True) if paragraph else {})})
        if not runs and para.empty():
            values.pop(target.node_id,None)
        else:
            values[target.node_id] = target.anchor(document.parsed.snapshot.content_hash,runs,para)
    return validate_formats([*values.values(), *(p for p in unresolved if p.node_id not in selected_ids)])


def character_css(fmt, css_string):
    parts = []
    if fmt.font_latin or fmt.font_zh:
        families = [css_string(fmt.font_latin) if fmt.font_latin else "var(--lf-font-latin)",
                    css_string(fmt.font_zh) if fmt.font_zh else "var(--lf-font-zh)"]
        parts.append("font-family:" + ",".join(families) + ";")
    if fmt.size_pt is not None:
        parts.append(f"font-size:{fmt.size_pt:g}pt;")
    if fmt.letter_spacing_pt is not None:
        parts.append(f"letter-spacing:{fmt.letter_spacing_pt:g}pt;")
    for value, rule in ((fmt.bold,"font-weight"),(fmt.italic,"font-style"),(fmt.underline,"text-decoration")):
        if value is not None:
            choices = {"font-weight":("400","700"),"font-style":("normal","italic"),"text-decoration":("none","underline")}
            parts.append(rule + ":" + choices[rule][int(value)] + ";")
    return "".join(parts)


def preview_projection(document, values, css_string, fallbacks=()):
    """Trusted spans carry offsets; source-provided attributes never reach this map."""
    targets, resolved, unresolved, issues = resolve_formats(document,values)
    ast = document.ast.to_dict()
    by_path = {t.path:(f"lf{i}",t) for i,t in enumerate(targets)}
    by_id = {p.node_id:p for p in resolved}
    css = []
    next_class = 0
    run_ends = {p.node_id:tuple(r.end for r in p.runs) for p in resolved}
    def leaf(node, start, text, override):
        nonlocal next_class
        if node["t"] == "Math":
            return {"t":"Span","c":[["",["local-object"],[["data-lf-start",str(start)],["data-lf-end",str(start+1)]]],[copy.deepcopy(node)]]}
        pieces = []
        active = []
        index = bisect_right(run_ends.get(override.node_id,()),start)
        for i in range(index,len(override.runs)):
            run = override.runs[i]
            if run.start >= start+len(text):
                break
            active.append(run)
        cuts = sorted({start,start+len(text), *(max(start,r.start) for r in active),*(min(start+len(text),r.end) for r in active)})
        for left,right in zip(cuts,cuts[1:]):
            classes = []
            fmt = next((r.format for r in active if r.start <= left and right <= r.end),None)
            if fmt:
                name = f"lf-c{next_class}"
                next_class += 1
                classes.append(name)
                css.append(f"#paper .local-block span.{name},#paper .local-block span.{name}>code{{{character_css(fmt,css_string)}}}")
            content = text[left-start:right-start]
            if node["t"] == "Code":
                child = {"t":"Code","c":[node["c"][0],content]}
            elif node["t"] == "LineBreak":
                child = {"t":"LineBreak"}
            else:
                child = {"t":"Str","c":content}
            pieces.append({"t":"Span","c":[["",classes,[["data-lf-start",str(left)],["data-lf-end",str(right)]]],[child]]})
        return {"t":"Span","c":[["",[],[]],pieces]} if len(pieces) != 1 else pieces[0]
    def inlines(nodes,path,override,cursor=0):
        result = []
        for index,node in enumerate(nodes):
            t,c = node["t"],node.get("c")
            p = path+(index,)
            if t in {"Str","Space","SoftBreak","LineBreak","Code","Math"}:
                if t == "Math" and node_id(p) in fallbacks:
                    result.append({"t":"Span","c":[["",["local-object"],[["data-lf-start",str(cursor)],["data-lf-end",str(cursor+1)]]],[{"t":"Code","c":[["",[],[]],c[1]]}]]})
                    cursor += 1
                    continue
                text = c if t == "Str" else c[1] if t == "Code" else "\ufffc" if t == "Math" else "\n" if t == "LineBreak" else " "
                result.append(leaf(node,cursor,text,override))
                cursor += len(text)
            elif t in {"Strong","Emph","Strikeout","Underline","Superscript","Subscript","SmallCaps"}:
                new,cursor = inlines(c,p+("c",),override,cursor)
                result.append({"t":t,"c":new})
            elif t in {"Link","Span","Cite","Quoted"}:
                if t == "Quoted":
                    left,right = ("‘","’") if c[0]["t"] == "SingleQuote" else ("“","”")
                    result.append(leaf({"t":"Str","c":left},cursor,left,override))
                    cursor += 1
                new,cursor = inlines(c[1],p+("c",1),override,cursor)
                if t == "Quoted":
                    result.extend(new)
                    result.append(leaf({"t":"Str","c":right},cursor,right,override))
                    cursor += 1
                else:
                    value = copy.deepcopy(node)
                    value["c"][1] = new
                    result.append(value)
        return result,cursor
    def visit(value,path=()):
        if isinstance(value,dict):
            if path in by_path:
                token,target = by_path[path]
                override = by_id.get(target.node_id,target.anchor(document.parsed.snapshot.content_hash))
                raw = copy.deepcopy(value)
                if target.kind == "CodeBlock":
                    # Keep pre/code semantics; an inline Code is whitespace preserving.
                    content = leaf({"t":"Code","c":[["",[],[]],target.text]},0,target.text,override)
                    raw = {"t":"Para","c":[content]}
                else:
                    key = 2 if target.kind == "Header" else None
                    nodes = raw["c"][key] if key is not None else raw["c"]
                    new,_ = inlines(nodes,path+(("c",key) if key is not None else ("c",)),override)
                    if key is None:
                        raw["c"] = new
                    else:
                        raw["c"][key] = new
                para = override.paragraph
                rules = []
                if para.line_spacing is not None:
                    line = para.line_spacing.multiplier if para.line_spacing.mode == "multiple" else f"{para.line_spacing.points:g}pt"
                    rules.append(f"line-height:{line};")
                if para.before_pt is not None:
                    rules.append(f"margin-top:{para.before_pt:g}pt;")
                if para.after_pt is not None:
                    rules.append(f"margin-bottom:{para.after_pt:g}pt;")
                if para.alignment:
                    rules.append(f"text-align:{para.alignment};")
                selector = f'#paper div[data-lf-block="{token}"]'
                if rules:
                    css.append(selector + ("" if target.kind == "Plain" else ">:first-child") + "{" + "".join(rules) + "}")
                if target.kind == "CodeBlock":
                    css.append(selector+"{white-space:pre-wrap;}"+selector+" code{white-space:pre-wrap;}")
                classes = ["local-block"] + (["local-code"] if target.kind == "CodeBlock" else [])
                return {"t":"Div","c":[["",classes,[["data-lf-block",token]]],[raw]]}
            return {k:visit(v,path+(k,)) for k,v in value.items()}
        if isinstance(value,list):
            return [visit(v,path+(i,)) for i,v in enumerate(value)]
        return value
    return visit(ast),"\n".join(css),targets,resolved,unresolved,issues
