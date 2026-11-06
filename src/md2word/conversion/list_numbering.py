"""Restore only Pandoc's missing numbering on verified display-only list items."""
from lxml import etree as ET

from ..domain.models import failure
from ..templates.word_styles import child, paragraph_properties
from .ooxml import NS, W, q
from .validation import Reader


def restore_display_list_numbering(package, paragraphs, targets):
    reader = Reader(package)
    groups, candidates, rules = {}, [], {}
    for paragraph, target in zip(paragraphs, targets):
        rule = target.numbering
        if rule is None:
            continue
        rules.setdefault(rule.group, rule)
        prop = reader.property(paragraph, "numPr")
        num = prop.find(q(W, "numId")) if prop is not None else None
        num_id = num.get(q(W, "val")) if num is not None else None
        if num_id not in (None, "0"):
            if rule.group in groups and groups[rule.group] != num_id:
                raise failure("CONTENT_MISMATCH", "列表编号实例不一致。", "停止写出并检查列表。", "export")
            groups[rule.group] = num_id
        elif target.kind == "display" and len(target.atoms) == 1 and target.atoms[0][0] == "math":
            # The independent atom reader verifies the formula before any repair.
            if reader.paragraph_atoms(paragraph) != target.atoms:
                raise failure("CONTENT_MISMATCH", "公式列表内容无法对应。", "停止写出并检查公式。", "export")
            candidates.append((paragraph, rule))
    if not candidates:
        return False

    numbering = package.numbering
    def next_id(tag, attr):
        return str(max((int(n.get(q(W, attr))) for n in numbering.findall(q(W, tag))), default=0) + 1)

    for _, rule in candidates:
        if rule.group in groups:
            continue
        first = rules[rule.group]
        abstract_id = next_id("abstractNum", "abstractNumId")
        abstract = ET.Element(q(W, "abstractNum"), {q(W, "abstractNumId"): abstract_id})
        ET.SubElement(abstract, q(W, "multiLevelType"), {q(W, "val"): "multilevel"})
        for depth in range(9):
            active = depth == first.level
            level = ET.SubElement(abstract, q(W, "lvl"), {q(W, "ilvl"): str(depth)})
            ET.SubElement(level, q(W, "start"), {q(W, "val"): str(first.number if active else 1)})
            fmt = first.format if active else "decimal"
            label = ("\u2022" if fmt == "bullet" else first.label) if active else f"%{depth + 1}."
            ET.SubElement(level, q(W, "numFmt"), {q(W, "val"): fmt})
            ET.SubElement(level, q(W, "lvlText"), {q(W, "val"): label})
            ET.SubElement(level, q(W, "lvlJc"), {q(W, "val"): "left"})
            ET.SubElement(level, q(W, "pPr"))
        # Abstract definitions precede concrete num instances in the schema.
        index = next((i for i, n in enumerate(numbering) if n.tag == q(W, "num")), len(numbering))
        numbering.insert(index, abstract)
        num_id = next_id("num", "numId")
        num = ET.SubElement(numbering, q(W, "num"), {q(W, "numId"): num_id})
        ET.SubElement(num, q(W, "abstractNumId"), {q(W, "val"): abstract_id})
        groups[rule.group] = num_id

    for paragraph, rule in candidates:
        prop = paragraph_properties(paragraph)
        num = child(prop, "numPr")
        child(num, "ilvl", {"val": rule.level})
        child(num, "numId", {"val": groups[rule.group]})
    return True

