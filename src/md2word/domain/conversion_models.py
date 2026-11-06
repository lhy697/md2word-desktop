from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from .style_models import ResolvedStyle
from .models import (ContentManifest, FrozenAst, Issue, NormalizedDocument,
                     SourceLocation, Transformation, Severity, failure)

class MathStatus(StrEnum):
    UNCHECKED = "unchecked"
    STRUCTURALLY_PASSED = "structurally_passed"
    SOURCE_FALLBACK = "source_fallback"
    UNCERTAIN = "uncertain"

@dataclass(frozen=True)
class MathRecord:
    node_id: str
    ordinal: int
    math_type: str
    source: str
    location: SourceLocation
    status: MathStatus = MathStatus.UNCHECKED
    signature: str | None = None
    cache_key: str = ""
    diagnostics: tuple[str, ...] = ()

@dataclass(frozen=True)
class CheckedDocument:
    normalized: NormalizedDocument
    ast: FrozenAst
    manifest: ContentManifest
    math_records: tuple[MathRecord, ...]
    math_fallbacks: tuple[Transformation, ...]
    issues: tuple[Issue, ...]
    probes: int
    cache_hits: int
    strategy_version: str = "math-probe-v1"
    reference_key: str = "base"

    @property
    def ready(self):
        return self.normalized.exportable and not any(
            i.severity == Severity.BLOCKING for i in self.issues) and all(
            m.status in {MathStatus.STRUCTURALLY_PASSED, MathStatus.SOURCE_FALLBACK}
            for m in self.math_records)

    def require_ready(self):
        if not self.ready:
            raise failure("MATH_UNCERTAIN", "尚不能可靠确认内容与公式的转换结果。",
                          "查看问题清单并处理后重试。", "math")

@dataclass(frozen=True)
class ValidationReport:
    passed: bool
    issues: tuple[Issue, ...]
    paragraphs: int = 0
    tables: int = 0
    math_objects: int = 0
    checked_atoms: int = 0

@dataclass(frozen=True)
class TemporaryConversion:
    task_dir: Path
    docx_path: Path
    checked: CheckedDocument
    validation: ValidationReport
    elapsed_seconds: float
    sha256: str
    style: ResolvedStyle | None = None
