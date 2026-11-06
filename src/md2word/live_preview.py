"""Versioned, bounded preview lane; shared Pandoc AST, no widgets or HTTP server."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import json
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .domain.ast_index import at_path
from .domain.models import DocumentError, InputSnapshot, Issue, Severity, failure
from .domain.normalizer import normalize
from .domain.style_models import ResolvedStyle
from .parsing import Parser
from .pandoc import PandocError
from .templates.fonts import check_fonts,check_local_fonts


def external_link(value):
    try:
        parts = urlsplit(value)
        return (parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
                and not parts.username and not parts.password
                and not any(ord(c) < 32 for c in value))
    except ValueError:
        return False


class SafeHtml(HTMLParser):
    """Allow structure, text and approved links only; never pass active attributes."""
    TAGS = frozenset("p br h1 h2 h3 h4 h5 h6 strong em s del sup sub code pre blockquote ul ol li table thead tbody tfoot tr th td caption span div hr a".split())
    VOID = {"br", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS:
            return
        clean = []
        for key, value in attrs:
            value = value or ""
            if tag == "a" and key == "href" and external_link(value):
                clean.append((key, value))
            elif key == "class" and tag in {"span", "div", "code", "pre"}:
                classes = [x for x in value.split() if x in {"math", "inline", "display", "sourceCode", "local-block", "local-code", "local-object"}
                           or re.fullmatch(r"lf-c[0-9]{1,6}",x)]
                if classes:
                    clean.append((key, " ".join(classes)))
            elif key in {"start", "colspan", "rowspan"} and value.isdecimal() and len(value) <= 5:
                clean.append((key, value))
            elif key == "title":
                clean.append((key, value))
            elif key == "data-lf-block" and tag == "div" and re.fullmatch(r"lf[0-9]{1,6}",value):
                clean.append((key,value))
            elif key in {"data-lf-start","data-lf-end"} and tag == "span" and re.fullmatch(r"[0-9]{1,7}",value):
                clean.append((key,value))
        self.parts.append("<" + tag + "".join(' ' + k + '="' + escape(v, quote=True) + '"' for k, v in clean) + ">")

    def handle_endtag(self, tag):
        if tag in self.TAGS and tag not in self.VOID:
            self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(escape(data))

    @classmethod
    def clean(cls, text):
        parser = cls()
        parser.feed(text)
        parser.close()
        return "".join(parser.parts)


def css_string(value):
    return '"' + "".join(c if c.isalnum() or c in " -" else "\\" + format(ord(c), "x") + " " for c in value) + '"'


def style_css(style):
    c = style.config
    def paragraph(p):
        line = str(p.line_spacing.multiplier) if p.line_spacing.mode == "multiple" else f"{p.line_spacing.points}pt"
        return (f"font-family:{css_string(p.font_latin)},{css_string(p.font_zh)},serif;"
                f"font-size:{p.size_pt}pt;line-height:{line};margin-top:{p.before_pt}pt;margin-bottom:{p.after_pt}pt;")
    rules = [f"#paper{{width:{c.page.width_mm}mm;min-height:{c.page.height_mm}mm;"
             f"padding:{c.page.top_mm}mm {c.page.right_mm}mm {c.page.bottom_mm}mm {c.page.left_mm}mm;"
             + paragraph(c.body) + "}",
             f"#paper>p,#paper>.local-block:not(.local-code)>p{{text-indent:{c.body.first_line_chars}em}}",
             f"#paper table{{{paragraph(c.table)}border-color:#{c.table.border_color}}}",
             f"#paper th{{background:#{c.table.header_fill}}}",
             f"#paper td,#paper th{{padding:{c.table.cell_vertical_mm}mm {c.table.cell_horizontal_mm}mm}}",
             f"#paper pre{{{paragraph(c.code)}background:#{c.code.fill};padding:12px;text-indent:0}}",
             f"#paper blockquote{{{paragraph(c.quote)}margin-left:{c.quote.indent_mm}mm}}",
             f"#paper ul,#paper ol{{padding-left:{c.lists.indent_mm}mm}}",
             f"#paper .math{{font-family:{css_string(c.math.font)},serif}}",
             f"#paper .math.display{{display:block;text-align:center;margin:{c.math.before_pt}pt 0 {c.math.after_pt}pt}}"]
    rules.extend(f"#paper h{h.level}{{{paragraph(h)}text-indent:0;break-after:avoid}}" for h in c.headings)
    for selector,fmt in [("#paper",c.body),("#paper table",c.table),("#paper blockquote",c.quote),("#paper code",c.code),("#paper .local-code",c.code),
                         *((f"#paper h{h.level}",h) for h in c.headings)]:
        rules.append(selector+"{"+f"--lf-font-latin:{css_string(fmt.font_latin)};--lf-font-zh:{css_string(fmt.font_zh)};"+"}")
    rules.append("#paper .local-code{"+paragraph(c.code)+f"background:#{c.code.fill};padding:12px;text-indent:0;"+"}")
    rules.append("#paper .local-code p{margin:0;text-indent:0;}#paper .local-code code{font:inherit;white-space:pre-wrap;}")
    rules.append("#paper a{text-decoration:none}#paper a span[data-lf-start]{text-decoration:underline}")
    return "\n".join(rules)


@dataclass(frozen=True)
class PreviewKey:
    session_id: str
    content_revision: int
    style_revision: int
    reader: str
    style_hash: str
    template_version: str
    fallbacks: tuple[str, ...] = ()
    local_hash: str = ""


@dataclass(frozen=True)
class PreviewRequest:
    key: PreviewKey
    snapshot: InputSnapshot
    style: ResolvedStyle
    local_formats: tuple = ()


@dataclass(frozen=True)
class PreviewResult:
    key: PreviewKey
    html: str = ""
    css: str = ""
    document: object = None
    issues: tuple[Issue, ...] = ()
    math_unchecked: int = 0
    failure: Issue | None = None
    local_targets: tuple = ()
    local_resolved: tuple = ()
    local_unresolved: tuple = ()


class PreviewService:
    def __init__(self, parser=None):
        self.parser = parser or Parser()
        self._cached = None
        self._font_hash = None
        self._font_issues = ()
        self.parse_count = 0

    def render(self, request, cancel):
        deadline = time.monotonic() + 15
        try:
            snapshot = request.snapshot
            if self._cached is None or self._cached.parsed.snapshot != snapshot:
                self._cached = normalize(self.parser.parse(snapshot, deadline=deadline, cancel=cancel), deadline=deadline, cancel=cancel)
                self.parse_count += 1
            doc = self._cached
            if not doc.exportable:
                return PreviewResult(request.key, document=doc, issues=doc.issues,
                                     failure=next(i for i in doc.issues if i.severity == Severity.BLOCKING))
            from .domain.local_formatting import preview_projection
            ast,local_css,targets,resolved,unresolved,local_issues = preview_projection(doc,request.local_formats,css_string,request.key.fallbacks)
            unchecked = sum(info.node_type == "Math" and info.node_id not in request.key.fallbacks for info in doc.node_index)
            with tempfile.TemporaryDirectory(prefix="md2word-html-") as temp:
                from pathlib import Path
                result = self.parser.runner.run(
                    ["--from=json", "--to=html5", "--mathjax", "--wrap=none", "--no-highlight"],
                    cwd=Path(temp), stdin=json.dumps(ast, ensure_ascii=False).encode("utf-8"),
                    deadline=deadline, cancel=cancel)
            if self._font_hash != request.style.resolved_hash:
                self._font_issues = check_fonts(request.style).issues
                self._font_hash = request.style.resolved_hash
            css = style_css(request.style)
            css += "\n" + local_css
            if snapshot.mode.value == "plain":
                css += "\n#paper p{white-space:pre-wrap}"
            return PreviewResult(request.key, SafeHtml.clean(result.stdout.decode("utf-8")),
                                 css, doc, doc.issues + self._font_issues + local_issues + check_local_fonts(resolved), unchecked,
                                 local_targets=targets, local_resolved=resolved, local_unresolved=unresolved)
        except (DocumentError, PandocError) as exc:
            issue = exc.issue if isinstance(exc, DocumentError) else Issue(
                "PREVIEW_FAILED", Severity.BLOCKING, "preview", "预览未更新，保留上次结果。", "检查输入后刷新预览。")
            return PreviewResult(request.key, failure=issue, issues=(issue,))
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            issue = Issue("PREVIEW_FAILED", Severity.BLOCKING, "preview", "预览未更新，原文仍保留。", "检查本地资源后重试。")
            return PreviewResult(request.key, failure=issue, issues=(issue,))


class PreviewLane:
    """Only the UI thread calls request/poll. The worker receives frozen requests."""
    def __init__(self, service=None):
        self.service = service or PreviewService()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="md2word-preview")
        self.active = None
        self.active_request = None
        self.pending = None
        self.cancel = threading.Event()
        self.closed = False

    def request(self, request):
        if self.closed:
            return
        if self.active is None:
            self._start(request)
        else:
            self.pending = request
            self.cancel.set()

    def _start(self, request):
        self.active_request = request
        self.cancel = threading.Event()
        self.active = self.pool.submit(self.service.render, request, self.cancel)

    def poll(self):
        if self.active is None or not self.active.done():
            return None
        try:
            result = self.active.result()
        except Exception:
            # Never surface a worker traceback or private input in the GUI.
            issue = Issue("PREVIEW_FAILED", Severity.BLOCKING, "preview", "预览未更新，原文仍保留。", "重新刷新预览。")
            result = PreviewResult(self.active_request.key, failure=issue, issues=(issue,))
        self.active = None
        pending, self.pending = self.pending, None
        if pending is not None and not self.closed:
            self._start(pending)
        return result

    @property
    def idle(self):
        return self.active is None or self.active.done()

    def shutdown(self):
        self.closed = True
        self.pending = None
        self.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
