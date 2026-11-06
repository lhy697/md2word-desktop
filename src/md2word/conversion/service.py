"""Stage-03 temporary artifacts. GUI scheduling and user commit are later stages."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import replace

from ..domain.conversion_models import TemporaryConversion
from ..domain.models import failure
from ..domain.normalizer import normalize
from ..parsing import Parser
from .common import check_budget
from .math_check import MathChecker
from .writer import DocxWriter

def cleanup_owned_task(task: Path, root: Path, owner: str) -> bool:
    """Fail closed around junctions/symlinks and files not created by this task."""
    try:
        if task.is_symlink() or task.parent.resolve() != root.resolve():
            return False
        marker = task / ".md2word-task.json"
        if json.loads(marker.read_text("utf-8")) != {"owner": owner, "stage": "03"}:
            return False
        for current, directories, files in os.walk(task, followlinks=False):
            for name in [*directories, *files]:
                path = Path(current) / name
                info = path.lstat()
                if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return False
        shutil.rmtree(task)
        return True
    except (OSError, ValueError):
        return False

class ConversionService:
    def __init__(self, parser=None, checker=None, writer=None):
        self.parser = parser or Parser()
        self.checker = checker or MathChecker(self.parser.runner)
        self.writer = writer or DocxWriter(self.parser.runner)

    def convert_temp(self, snapshot, *, work_root: Path | None = None,
                     deadline=None, cancel=None, style=None):
        if style is not None:
            from ..domain.style_models import ResolvedStyle
            if not isinstance(style, ResolvedStyle):
                raise failure("STYLE_INVALID", "需要有效样式快照。", "重新解析样式配置。", "style")
        started = time.monotonic()
        deadline = deadline if deadline is not None else started + 60
        check_budget(deadline, cancel)
        root = (work_root or Path(tempfile.gettempdir()) / "md2word-stage3").resolve()
        root.mkdir(parents=True, exist_ok=True)
        task = Path(tempfile.mkdtemp(prefix="job-", dir=root)).resolve()
        owner = str(uuid.uuid4())
        (task / ".md2word-task.json").write_text(json.dumps({"owner": owner, "stage": "03"}), "utf-8")
        try:
            parsed = self.parser.parse(snapshot, cwd=task, deadline=deadline, cancel=cancel)
            document = normalize(parsed)
            check_budget(deadline, cancel)
            checked = self.checker.check_math(document, work_dir=task,
                                              deadline=deadline, cancel=cancel,
                                              reference_key="lecture" if style is not None else "base")
            checked.require_ready()
            target, report = self.writer.write_docx(checked, work_dir=task,
                                                    deadline=deadline, cancel=cancel)
            if style is not None:
                from .postprocess import postprocess
                from ..templates.fonts import check_fonts
                styled = postprocess(target, task / "formatted.docx", checked, style,
                                     deadline=deadline, cancel=cancel)
                font_report = check_fonts(style)
                checked = replace(checked, issues=checked.issues + font_report.issues + styled.issues)
                target, report = styled.path, styled.validation
            fingerprint = hashlib.sha256(target.read_bytes()).hexdigest()
            return TemporaryConversion(task, target, checked, report,
                                       time.monotonic() - started, fingerprint, style)
        except BaseException as exc:
            if not cleanup_owned_task(task, root, owner):
                # Safe diagnostics can expose the owned task path on an explicit
                # development command; never upload it or log the source content.
                exc.task_dir = task
            raise
