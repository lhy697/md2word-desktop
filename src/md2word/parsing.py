"""Application entry for stage-02 parsing; no widgets or user-file writes."""
import json
from pathlib import Path
import re
import tempfile
import time

from .domain.ast_index import index_ast, node_id, validate_ast
from .domain.content_manifest import build_manifest
from .domain.models import (DocumentError, FrozenAst, InputMode, InputSnapshot, Issue,
                            ParsedDocument, ReaderProfile, Severity, SourceSpan,
                            digest, failure)
from .domain.source_mapping import build_source_map
from .input import check_text
from .pandoc import PandocError, PandocRunner
from .resources import ResourceLocator

def load_reader_profile(resources: ResourceLocator | None = None) -> ReaderProfile:
    try:
        raw = json.loads((resources or ResourceLocator()).file("reader-profile.json").read_text("utf-8"))
        return ReaderProfile.from_dict(raw)
    except (KeyError, TypeError, ValueError, OSError):
        raise failure("READER_PROFILE_INVALID", "转换配置不可读取或不一致。",
                      "恢复随程序提供的资源配置。") from None

def plain_ast(snapshot: InputSnapshot, api: tuple[int, ...]):
    blocks, spans, current, start = [], {}, [], None
    def flush(end):
        nonlocal current, start
        i = len(blocks)
        blocks.append({"t": "Para", "c": [item for item, _ in current]})
        for j, (_, span) in enumerate(current):
            spans[node_id(("blocks", i, "c", j))] = span
        if start is not None:
            spans[node_id(("blocks", i))] = SourceSpan(start, end)
        current, start = [], None
    parts = list(re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", snapshot.text))
    previous_break = None
    for match in parts:
        if not match[0]:
            continue
        full = match[0]
        line = full.rstrip("\r\n")
        line_end = match.start() + len(line)
        separator = SourceSpan(line_end, match.end())
        if line:
            if start is None:
                start = match.start()
            if current and previous_break:
                current.append(({"t": "LineBreak"}, previous_break))
            current.append(({"t": "Str", "c": line}, SourceSpan(match.start(), line_end)))
        else:
            flush(match.start())
        previous_break = separator if separator.start < separator.end else None
    if current:
        # A terminal newline remains an explicit break in the last paragraph.
        if previous_break and previous_break.end == len(snapshot.text):
            current.append(({"t": "LineBreak"}, previous_break))
        flush(len(snapshot.text))
    return {"pandoc-api-version": list(api), "meta": {}, "blocks": blocks}, spans

class Parser:
    def __init__(self, runner: PandocRunner | None = None,
                 profile: ReaderProfile | None = None):
        try:
            self.runner = runner or PandocRunner()
            self.profile = profile or load_reader_profile(self.runner.resources)
        except OSError:
            raise failure("RESOURCES_MISSING", "缺少必需的本地转换资源。",
                          "恢复随程序提供的资源文件。") from None

    def parse(self, snapshot: InputSnapshot, *, cwd: Path | None = None,
              deadline=None, cancel=None) -> ParsedDocument:
        deadline = deadline if deadline is not None else time.monotonic() + 15
        def check_task():
            if cancel is not None and cancel.is_set():
                raise failure("PARSE_CANCELLED", "解析已取消，原文仍保留。", "需要时重新检查。")
            if deadline is not None and time.monotonic() >= deadline:
                raise failure("PARSE_TIMEOUT", "解析已超时，原文仍保留。", "拆分文档或重试。")
        check_task()
        check_text(snapshot.text)
        if cwd is None:
            with tempfile.TemporaryDirectory(prefix="md2word-parse-") as temp:
                return self.parse(snapshot, cwd=Path(temp), deadline=deadline, cancel=cancel)
        exact = {}
        try:
            if snapshot.mode == InputMode.PLAIN:
                ast, exact = plain_ast(snapshot, self.profile.ast_api_version)
            else:
                ast = self.runner.parse(snapshot.text, cwd=cwd, profile=self.profile,
                                        deadline=deadline, cancel=cancel)
            validate_ast(ast, self.profile.ast_api_version)
            index = index_ast(ast)
            locations = build_source_map(snapshot, ast, index, exact)
            manifest = build_manifest(ast)
            frozen = FrozenAst.capture(ast)
            check_task()
        except DocumentError:
            raise
        except PandocError as exc:
            code = {"CANCELLED": "PARSE_CANCELLED", "EXPORT_TIMEOUT": "PARSE_TIMEOUT",
                    "AST_VERSION_UNSUPPORTED": "AST_VERSION_UNSUPPORTED",
                    "AST_INVALID": "AST_INVALID",
                    "ENGINE_VERSION_MISMATCH": "ENGINE_VERSION_MISMATCH",
                    "OUTPUT_LIMIT": "INPUT_COMPLEXITY"}.get(exc.code, "PARSE_FAILED")
            raise failure(code, "解析未完成，原始输入仍保留。",
                          "检查内容或重试；不会生成可导出的残缺文档。") from None
        except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise failure("PARSE_FAILED", "解析结果无法可靠读取，原始输入仍保留。",
                          "保留原文并重试；如重复出现，请反馈问题。") from None
        issues = []
        if not snapshot.text.strip():
            issues.append(Issue("INPUT_EMPTY", Severity.BLOCKING, "import",
                                "还没有可处理的正文。", "粘贴内容、导入文件或加载示例。"))
        return ParsedDocument(
            snapshot, self.profile,
            digest({"content": snapshot.content_hash, "reader": self.profile.fingerprint}),
            frozen, index, locations, manifest, tuple(issues))
