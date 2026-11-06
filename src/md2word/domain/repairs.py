"""Reviewable edits. Returning a plan does not change original or current input."""
from dataclasses import dataclass
import re
from .models import DocumentSession, InputSnapshot, InputMode, SourceSpan, failure

@dataclass(frozen=True)
class TextEdit:
    span: SourceSpan
    before: str
    after: str

@dataclass(frozen=True)
class RepairSuggestion:
    rule_id: str
    session_id: str
    revision: int
    content_hash: str
    edits: tuple[TextEdit, ...]
    before: str
    after: str
    action: str = "remove_outer_fence"

@dataclass(frozen=True)
class RepairReview:
    suggestion: RepairSuggestion | None
    reason: str | None = None

FENCE = re.compile(r"^( {0,3})(\x60{3,}|~{3,})([^\r\n]*)$")

def suggest_outer_fence(snapshot: InputSnapshot) -> RepairReview:
    if snapshot.mode != InputMode.MARKDOWN:
        return RepairReview(None)
    text = snapshot.text
    lines = text.splitlines(keepends=True)
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    nonempty = [i for i, x in enumerate(lines) if x.strip()]
    if len(nonempty) < 2:
        return RepairReview(None)
    first, last = nonempty[0], nonempty[-1]
    opening = FENCE.fullmatch(lines[first].rstrip("\r\n"))
    closing = FENCE.fullmatch(lines[last].rstrip("\r\n"))
    if not opening:
        return RepairReview(None)
    if opening[3].strip().lower() not in {"", "md", "markdown"}:
        return RepairReview(None, "外层围栏没有明确声明为 Markdown。")
    if not closing or closing[3].strip() or closing[2][0] != opening[2][0] or len(closing[2]) < len(opening[2]):
        return RepairReview(None, "外层围栏未闭合或不匹配。")
    if any(FENCE.fullmatch(line.rstrip("\r\n")) for line in lines[first + 1:last]):
        return RepairReview(None, "存在内部围栏，需要人工检查，未生成删除建议。")
    edits = (
        TextEdit(SourceSpan(offsets[first], offsets[first] + len(lines[first])), lines[first], ""),
        TextEdit(SourceSpan(offsets[last], offsets[last] + len(lines[last])), lines[last], ""),
    )
    after = text
    for edit in reversed(edits):
        after = after[:edit.span.start] + edit.after + after[edit.span.end:]
    return RepairReview(RepairSuggestion(
        "remove_outer_markdown_fence_v1", snapshot.session_id, snapshot.revision,
        snapshot.content_hash, edits, text, after))

def apply_repair(session: DocumentSession, suggestion: RepairSuggestion) -> DocumentSession:
    current = suggest_outer_fence(session.snapshot()).suggestion
    if (current is None or current != suggestion
            or suggestion.session_id != session.session_id
            or suggestion.revision != session.content_revision):
        raise failure("REPAIR_STALE", "修复建议已失效，当前内容未修改。",
                      "重新检查并确认新的修复建议。", "normalize")
    return session.edit(suggestion.after)

def undo_repair(session: DocumentSession, suggestion: RepairSuggestion) -> DocumentSession:
    if (session.session_id != suggestion.session_id
            or session.content_revision != suggestion.revision + 1
            or session.current_text != suggestion.after):
        raise failure("REPAIR_STALE", "内容已再次变化，不能应用旧修复的撤销。",
                      "使用当前编辑器的撤销历史。", "normalize")
    return session.edit(suggestion.before)
