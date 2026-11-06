"""Targeted Word formatting primitives. No paragraph text assignment or run rebuild."""
from lxml import etree as ET
from ..conversion.ooxml import M, NS, W, q
from ..domain.style_models import mm_to_twips, pt_to_twips, spacing_values

P_ORDER = ("pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr "
           "suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap "
           "overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd "
           "snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc "
           "textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange").split()
R_ORDER = ("rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow "
           "emboss imprint noProof snapToGrid vanish webHidden color spacing w kern "
           "position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs em "
           "lang eastAsianLayout specVanish oMath rPrChange").split()
S_ORDER = ("name aliases basedOn next link autoRedefine hidden uiPriority semiHidden "
           "unhideWhenUsed qFormat locked personal personalCompose personalReply rsid pPr rPr tblPr trPr tcPr").split()

def child(parent, name, attrs=None):
    value = parent.find(q(W, name))
    if value is None:
        value = ET.Element(q(W, name))
        order = P_ORDER if parent.tag == q(W, "pPr") else R_ORDER if parent.tag == q(W, "rPr") else S_ORDER if parent.tag == q(W, "style") else []
        ranks = {q(W, n): i for i, n in enumerate(order)}
        rank = ranks.get(value.tag, 10000)
        index = next((i for i, old in enumerate(parent) if ranks.get(old.tag, 10000) > rank), len(parent))
        parent.insert(index, value)
    for key, item in (attrs or {}).items():
        value.set(q(W, key), str(item))
    return value

def paragraph_properties(paragraph):
    prop = paragraph.find(q(W, "pPr"))
    if prop is None:
        prop = ET.Element(q(W, "pPr"))
        paragraph.insert(0, prop)
    return prop

def fonts(prop, font, size=True):
    node = child(prop, "rFonts", {"ascii": font.font_latin, "hAnsi": font.font_latin,
                                "eastAsia": font.font_zh, "cs": font.font_latin})
    for name in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme", "csTheme"):
        node.attrib.pop(q(W, name), None)
    if size:
        child(prop, "sz", {"val": round(font.size_pt * 2)})
        child(prop, "szCs", {"val": round(font.size_pt * 2)})

def paragraph_format(prop, config, *, first_chars=0, left_mm=0, right_mm=0):
    line, rule = spacing_values(config.line_spacing)
    child(prop, "spacing", {"before": pt_to_twips(config.before_pt),
                           "after": pt_to_twips(config.after_pt), "line": line, "lineRule": rule})
    indent = child(prop, "ind", {"left": mm_to_twips(left_mm), "right": mm_to_twips(right_mm)})
    for name in ("leftChars", "rightChars", "firstLine", "firstLineChars", "hanging", "hangingChars"):
        indent.attrib.pop(q(W, name), None)
    indent.set(q(W, "firstLineChars" if first_chars else "firstLine"),
               str(round(first_chars * 100)) if first_chars else "0")
    child(prop, "widowControl", {"val": 1})
    child(prop, "keepLines", {"val": 0})
    child(prop, "keepNext", {"val": 0})
    child(prop, "snapToGrid", {"val": 0})

def apply_page(section, config):
    child(section, "pgSz", {"w": mm_to_twips(config.width_mm), "h": mm_to_twips(config.height_mm), "orient": "portrait"})
    child(section, "pgMar", {
        "top": mm_to_twips(config.top_mm), "bottom": mm_to_twips(config.bottom_mm),
        "left": mm_to_twips(config.left_mm), "right": mm_to_twips(config.right_mm),
        "header": mm_to_twips(12), "footer": mm_to_twips(12), "gutter": 0})

def apply_styles(styles, config):
    by_id = {s.get(q(W, "styleId")): s for s in styles.findall(q(W, "style"))}
    def style(identity, name=None, kind="paragraph", base="Normal"):
        if identity not in by_id:
            value = ET.SubElement(styles, q(W, "style"), {q(W, "type"): kind, q(W, "styleId"): identity})
            child(value, "name", {"val": name or identity})
            if identity != base:
                child(value, "basedOn", {"val": base})
            by_id[identity] = value
        return by_id[identity]
    def paragraph(identity, fmt, first=0, left=0):
        value = style(identity)
        fonts(child(value, "rPr"), fmt)
        paragraph_format(child(value, "pPr"), fmt, first_chars=first, left_mm=left)
        return value
    for identity in ("Normal", "BodyText", "FirstParagraph"):
        paragraph(identity, config.body, config.body.first_line_chars if identity != "Normal" else 0)
    paragraph("Compact", config.body)
    paragraph("ListParagraph", config.body)
    for heading in config.headings:
        value = paragraph("Heading" + str(heading.level), heading)
        prop = child(value, "pPr")
        child(prop, "outlineLvl", {"val": heading.level - 1})
        child(prop, "keepNext", {"val": int(heading.keep_next)})
        child(prop, "keepLines", {"val": 1})
        child(prop, "pageBreakBefore", {"val": int(heading.page_break_before)})
        color = child(child(value, "rPr"), "color", {"val": "000000"})
        for attr in ("themeColor", "themeTint", "themeShade"):
            color.attrib.pop(q(W, attr), None)
        child(child(value, "rPr"), "b", {"val": int(heading.level <= 3)})
    code = paragraph("SourceCode", config.code)
    child(child(code, "pPr"), "shd", {"val": "clear", "fill": config.code.fill})
    child(child(code, "pPr"), "wordWrap", {"val": 0})  # OOXML off allows character-level wrapping.
    paragraph("BlockText", config.quote, left=config.quote.indent_mm)
    paragraph("TableBody", config.table)
    paragraph("TableHeader", config.table)
    paragraph("MathDisplay", config.body)
    prop = child(style("MathDisplay"), "pPr")
    child(prop, "jc", {"val": "center"})
    child(prop, "spacing", {"before": pt_to_twips(config.math.before_pt), "after": pt_to_twips(config.math.after_pt)})
    paragraph("MD2WordPageNumber", config.table)
    child(child(style("MD2WordPageNumber"), "pPr"), "jc", {"val": "center"})
    verbatim = style("VerbatimChar", "Verbatim Char", "character", "DefaultParagraphFont")
    fonts(child(verbatim, "rPr"), config.code)

def format_runs(paragraph, fmt, *, code=False, bold=False):
    for run in paragraph.findall(".//w:r", NS):
        if any(ancestor.tag == q(M, "oMath") for ancestor in run.iterancestors()):
            continue
        prop = run.find(q(W, "rPr"))
        run_style = prop.find(q(W, "rStyle")) if prop is not None else None
        if not code and run_style is not None and run_style.get(q(W, "val")) == "VerbatimChar":
            continue
        if prop is None:
            prop = ET.Element(q(W, "rPr"))
            run.insert(0, prop)
        fonts(prop, fmt)
        if bold:
            child(prop, "b", {"val": 1})

