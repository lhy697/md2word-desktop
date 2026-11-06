"""Controlled task-only DOCX writer; no user target or overwrite API."""
import os
from pathlib import Path
import time
import zipfile
from lxml import etree as ET

from ..domain.models import DocumentError, failure
from ..pandoc import PandocError, PandocRunner
from .common import check_budget, conversion_messages, read_log, remove_log
from .math_check import reference_info
from .ooxml import NS, W, Package, q
from .validation import validate_docx
from .write_plan import all_paragraphs, build_write_plan

def complete_writer_metadata(path: Path, plan):
    """Restore known omissions without weakening the final structural validator."""
    package = Package.read(path)
    changed = False
    def restore_empty(parent, targets):
        nonlocal changed
        children = [x for x in parent if x.tag in {q(W, "p"), q(W, "tbl")}]
        cursor = 0
        for target in targets:
            if getattr(target, "kind", None) == "empty":
                paragraph = ET.Element(q(W, "p"))
                if target.quote:
                    properties = ET.SubElement(paragraph, q(W, "pPr"))
                    ET.SubElement(properties, q(W, "pStyle"), {q(W, "val"): "BlockText"})
                before = children[cursor] if cursor < len(children) else parent.find(q(W, "sectPr"))
                if before is not None:
                    parent.insert(parent.index(before), paragraph)
                else:
                    parent.append(paragraph)
                changed = True
                continue
            if cursor >= len(children):
                raise failure("CONTENT_MISMATCH", "写出结构缺失。", "停止写出并检查结构。", "export")
            actual = children[cursor]
            cursor += 1
            if hasattr(target, "rows"):
                rows = actual.findall(q(W, "tr"))
                if len(rows) != len(target.rows):
                    raise failure("CONTENT_MISMATCH", "写出表格行数变化。", "停止写出并检查表格。", "export")
                for row, planned in zip(rows, target.rows):
                    cells = row.findall(q(W, "tc"))
                    if len(cells) != len(planned):
                        raise failure("CONTENT_MISMATCH", "写出表格列数变化。", "停止写出并检查表格。", "export")
                    for cell, blocks in zip(cells, planned):
                        restore_empty(cell, blocks)
        if cursor != len(children):
            raise failure("CONTENT_MISMATCH", "写出结构出现额外内容。", "停止写出并检查结构。", "export")
    restore_empty(package.document.find(q(W, "body")), plan.blocks)
    actual = package.document.findall(".//w:body//w:p", NS)
    expected = list(all_paragraphs(plan.blocks))
    if len(actual) != len(expected):
        raise failure("CONTENT_MISMATCH", "写出段落数量与原始结构不一致。",
                      "停止写出并检查结构。", "export")
    for paragraph, target in zip(actual, expected):
        links = paragraph.findall(".//w:hyperlink", NS)
        planned = [a for a in target.atoms if a[0] == "link_start"]
        if len(links) != len(planned):
            raise failure("CONTENT_MISMATCH", "写出链接无法与原始结构对应。",
                          "停止写出并检查链接。", "export")
        for link, (_, destination, title) in zip(links, planned):
            if package.hyperlink(link)[0] != destination:
                raise failure("CONTENT_MISMATCH", "链接目标发生了未允许的变化。",
                              "停止写出并检查链接。", "export")
            if title:
                link.set(q(W, "tooltip"), title)
                changed = True
        if target.kind == "math-source-display":
            properties = paragraph.find(q(W, "pPr"))
            if properties is None:
                properties = ET.Element(q(W, "pPr"))
                paragraph.insert(0, properties)
            jc = properties.find(q(W, "jc"))
            if jc is None:
                jc = ET.SubElement(properties, q(W, "jc"))
            jc.set(q(W, "val"), "center")
            changed = True
    from .list_numbering import restore_display_list_numbering
    numbering_changed = restore_display_list_numbering(package, actual, expected)
    if changed or numbering_changed:
        patch = path.with_suffix(".patch.docx")
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(patch, "x") as destination:
            for info in source.infolist():
                if info.filename == "word/document.xml":
                    data = ET.tostring(package.document, xml_declaration=True, encoding="UTF-8")
                elif numbering_changed and info.filename == "word/numbering.xml":
                    data = ET.tostring(package.numbering, xml_declaration=True, encoding="UTF-8")
                else:
                    data = source.read(info.filename)
                destination.writestr(info, data)
        # Replaces only our task's own intermediate, never a user destination.
        os.replace(patch, path)

class DocxWriter:
    def __init__(self, runner=None):
        self.runner = runner or PandocRunner()

    def write_docx(self, checked, *, work_dir: Path, deadline=None, cancel=None):
        deadline = deadline if deadline is not None else time.monotonic() + 60
        check_budget(deadline, cancel)
        plan = build_write_plan(checked)
        target = work_dir / "result.docx"
        intermediate = work_dir / ".building.docx"
        if any(p.exists() for p in (target, intermediate, intermediate.with_suffix(".patch.docx"),
                                     work_dir / "pandoc-log.json")):
            raise failure("OUTPUT_CONFLICT", "任务目录中已有输出，未覆盖。",
                          "为新任务创建独立目录。", "export")
        reference, _ = reference_info(self.runner, checked.reference_key)
        try:
            self.runner.write_docx(plan.ast.to_dict(), intermediate, cwd=work_dir,
                                   reference=reference,
                                   profile=checked.normalized.parsed.reader_profile,
                                   deadline=deadline, cancel=cancel)
            messages = conversion_messages(read_log(work_dir / "pandoc-log.json"), reference)
            if messages:
                raise failure("WRITER_UNCERTAIN", "正式写出出现未预期的转换诊断。",
                              "停止提交，保留原文并重新检查。", "export")
            complete_writer_metadata(intermediate, plan)
            check_budget(deadline, cancel)
            report = validate_docx(intermediate, checked, deadline=deadline, cancel=cancel)
            if not report.passed:
                raise DocumentError(report.issues[0])
            check_budget(deadline, cancel)
            # This is a newly owned task directory, not stage-05 user-file commit.
            if target.exists():
                raise failure("OUTPUT_CONFLICT", "任务输出冲突，未覆盖。",
                              "创建新任务目录后重试。", "export")
            intermediate.rename(target)
            return target, report
        except PandocError as exc:
            code = {"CANCELLED": "EXPORT_CANCELLED"}.get(exc.code, exc.code)
            raise failure(code, "Word 写出未完成，未提交文件。",
                          "保留原文并重试。", "export") from None
        finally:
            remove_log(work_dir / "pandoc-log.json")
            intermediate.unlink(missing_ok=True)
            intermediate.with_suffix(".patch.docx").unlink(missing_ok=True)
