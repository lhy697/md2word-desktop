"""Targeted local OOXML formatting with independent final property checks."""
from bisect import bisect_right
import copy

from lxml import etree as ET
from ..domain.ast_index import at_path, node_id
from ..domain.local_formatting import inline_parts, targets_for
from ..domain.models import failure
from ..domain.style_models import pt_to_twips, spacing_values
from ..templates.word_styles import child, paragraph_properties
from .ooxml import M, NS, W, q
from .write_plan import all_paragraphs, build_write_plan


def mismatch():
    return failure("LOCAL_FORMAT_MAPPING", "局部排版无法与导出文字逐项对应，未提交文件。", "刷新预览并重新选择文字后重试。", "style")


def expected_stream(checked, target,ast,math):
    node = at_path(ast,target.path)
    if target.kind == "CodeBlock":
        return target.text,list(range(len(target.text)))
    nodes = node["c"][2] if target.kind == "Header" else node["c"]
    path = target.path + (("c",2) if target.kind == "Header" else ("c",))
    chars,mapping = [],[]
    for leaf,p,start,text in inline_parts(nodes,path):
        if leaf["t"] == "Math":
            record = math.get(node_id(p))
            if record and record.status.value == "source_fallback":
                from .math_check import fallback_node
                value = fallback_node(record)["c"][1]
            else:
                value = "\ufffc"
            chars.append(value)
            mapping.extend([None]*len(value))
        else:
            chars.append(text)
            mapping.extend(range(start,start+len(text)))
    return "".join(chars),mapping


def run_text(run):
    pieces = []
    for node in run:
        if node.tag == q(W,"t"):
            pieces.append(node.text or "")
        elif node.tag == q(W,"tab"):
            pieces.append("\t")
        elif node.tag in {q(W,"br"),q(W,"cr")}:
            pieces.append("\n")
    return "".join(pieces)


def paragraph_runs(paragraph):
    cursor = 0
    for node in paragraph.iter():
        if node.tag == q(M,"oMath"):
            yield None,cursor,"\ufffc"
            cursor += 1
        elif node.tag == q(W,"r") and not any(a.tag == q(M,"oMath") for a in node.iterancestors()):
            text = run_text(node)
            yield node,cursor,text
            cursor += len(text)


def properties(fmt):
    values = {}
    if fmt.font_zh is not None:
        values[("rFonts","eastAsia")] = fmt.font_zh
    if fmt.font_latin is not None:
        for key in ("ascii","hAnsi","cs"):
            values[("rFonts",key)] = fmt.font_latin
    if fmt.size_pt is not None:
        values[("sz","val")] = values[("szCs","val")] = str(round(fmt.size_pt*2))
    if fmt.letter_spacing_pt is not None:
        values[("spacing","val")] = str(pt_to_twips(fmt.letter_spacing_pt))
    for name,value in (("b",fmt.bold),("i",fmt.italic),("u",fmt.underline)):
        if value is not None:
            values[(name,"val")] = ("single" if value else "none") if name == "u" else str(int(value))
    return values


def format_run(run,fmt):
    prop = child(run,"rPr")
    # rPr is the first child of a run, independent of style-property ordering.
    if run.index(prop):
        run.remove(prop)
        run.insert(0,prop)
    for (tag,attr),value in properties(fmt).items():
        node = child(prop,tag,{attr:value})
        if tag == "rFonts":
            for theme in ({"eastAsia":"eastAsiaTheme"}.get(attr), {"ascii":"asciiTheme","hAnsi":"hAnsiTheme","cs":"csTheme"}.get(attr)):
                if theme:
                    node.attrib.pop(q(W,theme),None)


def slice_run(run,left,right):
    clone = ET.Element(run.tag,dict(run.attrib))
    prop = run.find(q(W,"rPr"))
    if prop is not None:
        clone.append(copy.deepcopy(prop))
    cursor = 0
    for node in run:
        if node.tag == q(W,"rPr"):
            continue
        if node.tag == q(W,"t"):
            text = node.text or ""
            a,b = max(left,cursor),min(right,cursor+len(text))
            if a < b:
                if len(node):
                    raise mismatch()
                value = ET.Element(node.tag,dict(node.attrib))
                value.text = text[a-cursor:b-cursor]
                value.set("{http://www.w3.org/XML/1998/namespace}space","preserve")
                clone.append(value)
            cursor += len(text)
        elif node.tag in {q(W,"tab"),q(W,"br"),q(W,"cr")}:
            if left <= cursor < right:
                clone.append(copy.deepcopy(node))
            cursor += 1
        else:
            # Never rebuild a field, drawing, comment anchor or unknown run payload.
            raise mismatch()
    return clone


def paragraph_properties_expected(fmt):
    values = {}
    if fmt.before_pt is not None:
        values[("spacing","before")] = str(pt_to_twips(fmt.before_pt))
    if fmt.after_pt is not None:
        values[("spacing","after")] = str(pt_to_twips(fmt.after_pt))
    if fmt.line_spacing is not None:
        line,rule = spacing_values(fmt.line_spacing)
        values[("spacing","line")] = str(line)
        values[("spacing","lineRule")] = rule
    if fmt.alignment is not None:
        values[("jc","val")] = "both" if fmt.alignment == "justify" else fmt.alignment
    return values


def visit_local(document,checked,formats,*,apply,check_budget):
    by_id = {p.node_id:p for p in formats}
    targets = {t.node_id:t for t in targets_for(checked.normalized)}
    ast = checked.normalized.ast.to_dict()
    math = {r.node_id:r for r in checked.math_records}
    streams = {identity:expected_stream(checked,targets[identity],ast,math) for identity in by_id if identity in targets}
    if len(streams) != len(by_id):
        raise mismatch()
    plan = list(all_paragraphs(build_write_plan(checked).blocks))
    paragraphs = document.findall(".//w:body//w:p",NS)
    if len(plan) != len(paragraphs):
        raise mismatch()
    cursors = {}
    for paragraph,expected in zip(paragraphs,plan):
        value = by_id.get(expected.node_id)
        if value is None:
            continue
        check_budget()
        normal,mapping = streams[expected.node_id]
        segment = "".join(a[1] if a[0] == "text" else "\ufffc" if a[0] == "math" else "" for a in expected.atoms)
        cursor = cursors.get(expected.node_id,0)
        offset = normal.find(segment,cursor) if segment else cursor
        if offset < 0 or normal[cursor:offset].strip():
            raise mismatch()
        cursors[expected.node_id] = offset+len(segment)
        actual = list(paragraph_runs(paragraph))
        if "".join(item[2] for item in actual) != segment:
            raise mismatch()
        properties_map = paragraph_properties_expected(value.paragraph)
        prop = paragraph_properties(paragraph)
        for (tag,attr),wanted in properties_map.items():
            if apply:
                child(prop,tag,{attr:wanted})
            else:
                node = prop.find(q(W,tag))
                if node is None or node.get(q(W,attr)) != wanted:
                    raise mismatch()
        starts = [r.start for r in value.runs]
        def format_at(position):
            target_offset = mapping[offset+position]
            if target_offset is None:
                return None
            index = bisect_right(starts,target_offset)-1
            if index >= 0 and target_offset < value.runs[index].end:
                return value.runs[index].format
            return None
        for run,start,text in actual:
            if run is None or not text:
                continue
            groups = []
            for index in range(len(text)):
                fmt = format_at(start+index)
                if groups and groups[-1][2] is fmt:
                    groups[-1] = (groups[-1][0],index+1,fmt)
                else:
                    groups.append((index,index+1,fmt))
            if not any(fmt is not None for _,_,fmt in groups):
                continue
            if apply:
                parent = run.getparent()
                insertion = parent.index(run)
                for left,right,fmt in groups:
                    piece = slice_run(run,left,right)
                    if fmt is not None:
                        format_run(piece,fmt)
                    parent.insert(insertion,piece)
                    insertion += 1
                parent.remove(run)
            else:
                for _,_,fmt in groups:
                    if fmt is None:
                        continue
                    rpr = run.find(q(W,"rPr"))
                    if rpr is None:
                        raise mismatch()
                    for (tag,attr),wanted in properties(fmt).items():
                        node = rpr.find(q(W,tag))
                        if node is None or node.get(q(W,attr)) != wanted:
                            raise mismatch()
