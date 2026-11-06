"""One-formula probes with session-bounded LRU, not exit-code inference."""
from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import tempfile
import time

from ..domain.ast_index import at_path, validate_ast
from ..domain.content_manifest import build_manifest
from ..domain.conversion_models import CheckedDocument, MathRecord, MathStatus
from ..domain.models import (ContentManifest, FrozenAst, Issue, Severity, Transformation,
                             digest, failure, stable_json)
from ..domain.normalizer import audit_normalization
from ..pandoc import PandocError, PandocRunner
from .common import check_budget, read_log, remove_log, conversion_messages
from .ooxml import M, W, Package, math_signature, plain_word_text, q

STRATEGY_VERSION = "math-probe-v1"

@dataclass(frozen=True)
class ProbeOutcome:
    status: MathStatus
    signature: str | None
    diagnostics: tuple[str, ...]

class MathCache:
    def __init__(self, capacity=256):
        if not 1 <= capacity <= 4096:
            raise ValueError("Invalid cache capacity")
        self.capacity = capacity
        self.session_id = None
        self._values = OrderedDict()

    def bind(self, session_id):
        if self.session_id != session_id:
            self._values.clear()
            self.session_id = session_id

    def get(self, key):
        value = self._values.get(key)
        if value:
            self._values.move_to_end(key)
        return value

    def put(self, key, outcome):
        if outcome.status == MathStatus.UNCERTAIN:
            return
        self._values[key] = outcome
        self._values.move_to_end(key)
        while len(self._values) > self.capacity:
            self._values.popitem(last=False)

    def __len__(self):
        return len(self._values)

def reference_info(runner: PandocRunner, key="base"):
    resources = {"base": "reference-base", "lecture": "lecture-reference"}
    if key not in resources:
        raise failure("REFERENCE_INVALID", "未知模板资源。", "恢复内置模板。", "export")
    stem = resources[key]
    try:
        path = runner.resources.file("templates/" + stem + ".docx")
        meta = json.loads(runner.resources.file("templates/" + stem + ".json").read_text("utf-8"))
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        valid = meta.get("sha256") == actual
    except (OSError, ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        raise failure("REFERENCE_INVALID", "基础 Word 模板与锁定记录不一致。",
                      "恢复受控模板后重试。", "export")
    return path, actual

def extract_math(document) -> tuple[MathRecord, ...]:
    ast = document.ast.to_dict()
    locations = {x.node_id: x for x in document.source_map}
    records = []
    for info in document.node_index:
        if info.node_type == "Math":
            node = at_path(ast, info.path)
            records.append(MathRecord(info.node_id, len(records) + 1,
                                      node["c"][0]["t"], node["c"][1],
                                      locations[info.node_id]))
    return tuple(records)

def fallback_node(record: MathRecord):
    delimiter = "$" if record.math_type == "InlineMath" else "$$"
    kind = "math-source" if record.math_type == "InlineMath" else "math-source-display"
    return {"t": "Code", "c": [["", [kind], []], delimiter + record.source + delimiter]}

def checked_manifest(ast, records):
    manifest = build_manifest(ast)
    by_id = {x.node_id: x for x in records}
    entries = []
    for entry in manifest.entries:
        record = by_id.get(entry.entry_id)
        if record:
            payload = entry.payload
            payload["math_status"] = record.status.value
            if entry.kind == "Math":
                payload["status"] = record.status.value
            else:
                payload["math_source"] = record.source
                payload["math_type"] = record.math_type
            entry = replace(entry, payload_json=stable_json(payload))
        entries.append(entry)
    return ContentManifest(tuple(entries))

def audit_checked(checked: CheckedDocument):
    normalized = checked.normalized
    audit_normalization(normalized.parsed, normalized.ast.to_dict(),
                        normalized.fallback_records, normalized.manifest)
    expected = normalized.ast.to_dict()
    initial = extract_math(normalized)
    if len(initial) != len(checked.math_records):
        raise failure("CONTENT_MISMATCH", "公式记录数量无法与原始节点对应。",
                      "停止导出并重新检查。", "math")
    paths = {n.node_id: n.path for n in normalized.node_index}
    expected_changes = []
    for original, record in zip(initial, checked.math_records):
        if (original.node_id, original.ordinal, original.math_type, original.source, original.location) != (
                record.node_id, record.ordinal, record.math_type, record.source, record.location):
            raise failure("CONTENT_MISMATCH", "公式来源记录不一致。",
                          "停止导出并重新检查。", "math")
        if record.status == MathStatus.SOURCE_FALLBACK:
            path = paths[record.node_id]
            before = at_path(expected, path)
            after = fallback_node(record)
            expected_changes.append(Transformation(record.node_id, record.node_id, path,
                "math_source_visible_v1", stable_json(before), stable_json(after)))
            at_path(expected, path[:-1])[path[-1]] = after
        elif record.status == MathStatus.STRUCTURALLY_PASSED and not record.signature:
            raise failure("MATH_UNCERTAIN", "公式缺少可核对的结构记录。",
                          "重新检查公式。", "math")
    if (tuple(expected_changes) != checked.math_fallbacks
            or stable_json(expected) != stable_json(checked.ast.to_dict())
            or checked.manifest != checked_manifest(expected, checked.math_records)):
        raise failure("CONTENT_MISMATCH", "公式降级包含未声明的内容变化。",
                      "停止导出并保留原文。", "math")

class MathChecker:
    def __init__(self, runner=None, cache=None):
        self.runner = runner or PandocRunner()
        self.cache = cache if cache is not None else MathCache()

    def probe(self, record, profile, reference, work_dir, deadline, cancel):
        check_budget(deadline, cancel)
        # A single Math node provides unambiguous attribution without probe markers.
        ast = {"pandoc-api-version": list(profile.ast_api_version), "meta": {},
               "blocks": [{"t": "Para", "c": [
                   {"t": "Math", "c": [{"t": record.math_type}, record.source]}]}]}
        with tempfile.TemporaryDirectory(prefix="probe-", dir=work_dir) as temp:
            folder = Path(temp)
            target = folder / "probe.docx"
            try:
                self.runner.write_docx(ast, target, cwd=folder, reference=reference,
                                       profile=profile, deadline=deadline, cancel=cancel)
                logs = conversion_messages(read_log(folder / "pandoc-log.json"), reference)
                package = Package.read(target)
                body = package.document.find(q(W, "body"))
                equations = body.findall(".//" + q(M, "oMath"))
                visible = plain_word_text(body)
                diagnostic = tuple(sorted({str(x.get("type", "unknown")) for x in logs}))
                if (record.source.strip() and len(equations) == 1 and not logs
                        and any((x.text or "").strip() for x in equations[0].iter(q(M, "t")))
                        and not visible):
                    display = equations[0].getparent().tag == q(M, "oMathPara")
                    if display == (record.math_type == "DisplayMath"):
                        return ProbeOutcome(MathStatus.STRUCTURALLY_PASSED,
                                            math_signature(equations[0]), ())
                delimiter = "$" if record.math_type == "InlineMath" else "$$"
                if (not equations and logs
                        and all(x.get("type") == "CouldNotConvertTeXMath"
                                and x.get("contents") == record.source for x in logs)):
                    # This single-source probe proves conversion failure. Pandoc's
                    # native fallback may indent multiline text; the application
                    # instead writes the complete immutable source itself.
                    return ProbeOutcome(MathStatus.SOURCE_FALLBACK, None, diagnostic)
                return ProbeOutcome(MathStatus.UNCERTAIN, None, diagnostic or ("PROBE_AMBIGUOUS",))
            finally:
                remove_log(folder / "pandoc-log.json")

    def check_math(self, document, *, work_dir: Path, deadline=None, cancel=None, reference_key="base"):
        document.require_exportable()
        deadline = deadline if deadline is not None else time.monotonic() + 60
        check_budget(deadline, cancel)
        audit_normalization(document.parsed, document.ast.to_dict(),
                            document.fallback_records, document.manifest)
        try:
            reference, reference_hash = reference_info(self.runner, reference_key)
            self.runner.ensure_version(document.parsed.reader_profile, cwd=work_dir,
                                       deadline=deadline, cancel=cancel)
        except PandocError as exc:
            raise failure(exc.code, "转换引擎不可用或与配置不一致。",
                          "保留原文并恢复本地转换资源。", "math") from None
        self.cache.bind(document.parsed.snapshot.session_id)
        records, issues, changes = [], list(document.issues), []
        probes = hits = 0
        ast = document.ast.to_dict()
        paths = {n.node_id: n.path for n in document.node_index}
        for original in extract_math(document):
            check_budget(deadline, cancel)
            key = digest({"source": original.source, "type": original.math_type,
                          "engine": document.parsed.reader_profile.engine_version,
                          "reader": document.parsed.reader_profile.fingerprint,
                          "policy": document.policy_version, "strategy": STRATEGY_VERSION,
                          "reference": reference_hash})
            outcome = self.cache.get(key)
            if outcome is None:
                try:
                    outcome = self.probe(original, document.parsed.reader_profile,
                                         reference, work_dir, deadline, cancel)
                except PandocError as exc:
                    code = {"CANCELLED": "EXPORT_CANCELLED"}.get(exc.code, exc.code)
                    raise failure(code, "公式探测未完成，未提交文件。",
                                  "保留原文并重试。", "math") from None
                self.cache.put(key, outcome)
                probes += 1
            else:
                hits += 1
            record = replace(original, status=outcome.status, signature=outcome.signature,
                             cache_key=key, diagnostics=outcome.diagnostics)
            records.append(record)
            if record.status == MathStatus.SOURCE_FALLBACK:
                path = paths[record.node_id]
                before = at_path(ast, path)
                after = fallback_node(record)
                changes.append(Transformation(record.node_id, record.node_id, path,
                    "math_source_visible_v1", stable_json(before), stable_json(after)))
                at_path(ast, path[:-1])[path[-1]] = after
                issues.append(Issue("MATH_FALLBACK", Severity.WARNING, "math",
                    "该公式无法转换为 Word 公式，已保留可见源码。",
                    "查看源码，或改用支持的公式写法。", record.node_id, record.location))
            elif record.status == MathStatus.UNCERTAIN:
                issues.append(Issue("MATH_UNCERTAIN", Severity.BLOCKING, "math",
                    "该公式的转换结果不能可靠确认。",
                    "检查公式后重试，当前不会生成可提交成品。", record.node_id, record.location))
        validate_ast(ast, document.parsed.reader_profile.ast_api_version)
        checked = CheckedDocument(document, FrozenAst.capture(ast),
            checked_manifest(ast, records), tuple(records), tuple(changes),
            tuple(issues), probes, hits, STRATEGY_VERSION, reference_key)
        audit_checked(checked)
        check_budget(deadline, cancel)
        return checked

