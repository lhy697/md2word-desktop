from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import hashlib
import json
from typing import Any
import uuid

def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)

def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()

class InputMode(StrEnum):
    MARKDOWN = "markdown"
    PLAIN = "plain"

class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"

class Confidence(StrEnum):
    EXACT = "exact"
    UNIQUE_MATCH = "unique_match"
    CONTEXTUAL = "contextual"

@dataclass(frozen=True)
class SourceSpan:
    """Half-open Python Unicode code-point offsets in the unchanged input."""
    start: int
    end: int

    def __post_init__(self):
        if not (type(self.start) is int and type(self.end) is int
                and 0 <= self.start <= self.end):
            raise ValueError("Invalid source range")

@dataclass(frozen=True)
class SourceLocation:
    node_id: str
    confidence: Confidence
    span: SourceSpan | None
    section: str
    ordinal: int
    snippet: str
    session_id: str
    revision: int

    def __post_init__(self):
        if (self.confidence == Confidence.CONTEXTUAL) != (self.span is None):
            raise ValueError("Contextual locations cannot carry a jump range")

@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    stage: str
    message: str
    action: str
    node_id: str | None = None
    location: SourceLocation | None = None
    diagnostic_ref: str | None = None

class DocumentError(Exception):
    """Safe user-facing error; never embed the original subprocess message."""
    def __init__(self, issue: Issue):
        super().__init__(issue.message)
        self.issue = issue
        self.code = issue.code

def failure(code: str, message: str, action: str, stage: str = "parse") -> DocumentError:
    return DocumentError(Issue(code, Severity.BLOCKING, stage, message, action))

@dataclass(frozen=True)
class InputSnapshot:
    session_id: str
    revision: int
    text: str
    mode: InputMode
    content_hash: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.mode, InputMode) or self.revision < 0:
            raise ValueError("Invalid input snapshot")
        try:
            content_hash = digest({"text": self.text, "mode": self.mode.value})
        except UnicodeEncodeError:
            raise failure("INPUT_ENCODING", "输入包含无法编码的 Unicode 字符。",
                          "重新复制原文或另存为 UTF-8。", "import") from None
        object.__setattr__(self, "content_hash", content_hash)

@dataclass(frozen=True)
class DocumentSession:
    session_id: str
    original_text: str
    current_text: str
    input_mode: InputMode
    content_revision: int = 0
    style_revision: int = 0
    dirty: bool = False
    local_formats: tuple = ()
    local_hash: str = field(init=False, default="")

    def __post_init__(self):
        from .local_formatting import validate_formats
        values = validate_formats(self.local_formats)
        object.__setattr__(self, "local_formats", values)
        object.__setattr__(self, "local_hash", digest([v.model_dump(mode="json") for v in values]) if values else "")

    def format_selection(self, values):
        return replace(self, local_formats=values, style_revision=self.style_revision + 1, dirty=True)

    @classmethod
    def create(cls, text: str = "", mode: InputMode = InputMode.MARKDOWN):
        return cls(str(uuid.uuid4()), text, text, InputMode(mode))

    def edit(self, text: str) -> DocumentSession:
        if text == self.current_text:
            return self
        return replace(self, current_text=text,
                       content_revision=self.content_revision + 1, dirty=True)

    def set_mode(self, mode: InputMode) -> DocumentSession:
        mode = InputMode(mode)
        if mode == self.input_mode:
            return self
        return replace(self, input_mode=mode,
                       content_revision=self.content_revision + 1, dirty=True)

    def style_changed(self) -> DocumentSession:
        return replace(self, style_revision=self.style_revision + 1, dirty=True)

    def snapshot(self) -> InputSnapshot:
        return InputSnapshot(self.session_id, self.content_revision,
                             self.current_text, self.input_mode)

@dataclass(frozen=True)
class ReaderProfile:
    profile_version: str
    engine_version: str
    reader: str
    effective_extensions: tuple[tuple[str, bool], ...]
    ast_api_version: tuple[int, ...]
    fingerprint: str = field(init=False)

    @classmethod
    def from_dict(cls, raw: dict) -> ReaderProfile:
        extensions = raw["effective_extensions"]
        if (not isinstance(extensions, dict) or not extensions
                or not all(isinstance(k, str) and type(v) is bool
                           for k, v in extensions.items())):
            raise ValueError("Invalid extension switches")
        switches = tuple(sorted(extensions.items()))
        expected = "markdown" + "".join(("+" if v else "-") + k for k, v in switches)
        if expected != raw["reader"]:
            raise ValueError("Reader and explicit extensions disagree")
        for key in ("profile_version", "engine_version"):
            if not isinstance(raw[key], str) or not raw[key]:
                raise ValueError("Missing reader version")
        if not all(type(x) is int and x >= 0 for x in raw["ast_api_version"]):
            raise ValueError("Invalid AST API version")
        return cls(raw["profile_version"], raw["engine_version"], raw["reader"],
                   switches, tuple(raw["ast_api_version"]))

    def __post_init__(self):
        object.__setattr__(self, "fingerprint", digest({
            "version": self.profile_version, "engine": self.engine_version,
            "reader": self.reader, "extensions": self.effective_extensions,
            "api": self.ast_api_version}))

@dataclass(frozen=True)
class FrozenAst:
    """Serialize once; every to_dict() gives a detached mutable working copy."""
    _json: str = field(repr=False)

    @classmethod
    def capture(cls, ast: dict) -> FrozenAst:
        return cls(stable_json(ast))

    def to_dict(self) -> dict:
        return json.loads(self._json)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._json.encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    path: tuple[str | int, ...]
    node_type: str
    parent_id: str | None
    ordinal: int
    section: str

@dataclass(frozen=True)
class ManifestEntry:
    entry_id: str
    kind: str
    parent_id: str | None
    ordinal: int
    payload_json: str

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)

@dataclass(frozen=True)
class ContentManifest:
    entries: tuple[ManifestEntry, ...]

    def of_kind(self, kind: str) -> tuple[ManifestEntry, ...]:
        return tuple(x for x in self.entries if x.kind == kind)

@dataclass(frozen=True)
class ParsedDocument:
    snapshot: InputSnapshot
    reader_profile: ReaderProfile
    input_key: str
    ast: FrozenAst
    node_index: tuple[NodeInfo, ...]
    source_map: tuple[SourceLocation, ...]
    manifest: ContentManifest
    issues: tuple[Issue, ...]

    def location(self, node_id: str) -> SourceLocation:
        return next(x for x in self.source_map if x.node_id == node_id)

@dataclass(frozen=True)
class Transformation:
    origin_node_id: str
    target_node_id: str
    path: tuple[str | int, ...]
    rule: str
    before_json: str
    after_json: str

@dataclass(frozen=True)
class NodeOrigin:
    origin_node_id: str
    target_node_id: str
    relation: str

@dataclass(frozen=True)
class ManifestAudit:
    passed: bool
    source_entries: int
    normalized_entries: int
    transformed_roots: int

@dataclass(frozen=True)
class NormalizedDocument:
    parsed: ParsedDocument
    ast: FrozenAst
    node_index: tuple[NodeInfo, ...]
    source_map: tuple[SourceLocation, ...]
    origins: tuple[NodeOrigin, ...]
    fallback_records: tuple[Transformation, ...]
    source_manifest: ContentManifest
    manifest: ContentManifest
    audit: ManifestAudit
    issues: tuple[Issue, ...]
    policy_version: str = "support-v2"

    @property
    def exportable(self) -> bool:
        return self.audit.passed and not any(
            x.severity == Severity.BLOCKING for x in self.issues)

    def require_exportable(self) -> FrozenAst:
        if not self.exportable:
            raise failure("DOCUMENT_BLOCKED", "文档包含尚未解决的阻断问题。",
                          "查看问题清单，处理后重新检查。", "normalize")
        return self.ast
