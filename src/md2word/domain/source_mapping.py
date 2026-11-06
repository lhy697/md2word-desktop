"""Conservative literal association, never a second Markdown parser."""
from collections import Counter
from bisect import bisect_right
import re
from .ast_index import at_path, inline_text
from .models import (Confidence, InputSnapshot, NodeInfo, SourceLocation, SourceSpan, failure)

def normalized_newlines(text: str) -> tuple[str, tuple[int, ...]]:
    chars, offsets = [], []
    i = 0
    while i < len(text):
        offsets.append(i)
        if text[i] == "\r":
            chars.append("\n")
            i += 2 if text[i:i + 2] == "\r\n" else 1
        else:
            chars.append(text[i])
            i += 1
    offsets.append(len(text))
    return "".join(chars), tuple(offsets)

def to_qt_utf16(text: str, offset: int) -> int:
    """QPlainTextEdit uses LF and UTF-16, even if the imported file had CRLF."""
    if not 0 <= offset <= len(text):
        raise ValueError("Offset outside source")
    if 0 < offset < len(text) and text[offset - 1:offset + 1] == "\r\n":
        raise ValueError("Offset inside CRLF")
    normalized, _ = normalized_newlines(text[:offset])
    return len(normalized.encode("utf-16-le")) // 2

def from_qt_utf16(text: str, position: int) -> int:
    if position < 0:
        raise ValueError("Negative UTF-16 position")
    normal, offsets = normalized_newlines(text)
    current = 0
    for i, char in enumerate(normal):
        if current == position:
            return offsets[i]
        current += 2 if ord(char) > 0xFFFF else 1
        if current > position:
            raise ValueError("Position splits a surrogate pair")
    if current != position:
        raise ValueError("Position outside editor")
    return len(text)

def qt_selection(snapshot: InputSnapshot, location: SourceLocation) -> tuple[int, int] | None:
    """UI handoff: stale or contextual locations can never select current text."""
    if (location.session_id != snapshot.session_id or location.revision != snapshot.revision):
        raise failure("LOCATION_STALE", "定位信息属于旧版本，当前内容未改变。",
                      "重新检查当前内容后定位。", "normalize")
    if location.span is None:
        return None
    return (to_qt_utf16(snapshot.text, location.span.start),
            to_qt_utf16(snapshot.text, location.span.end))

def escaped(text: str, offset: int) -> bool:
    slashes, i = 0, offset - 1
    while i >= 0 and text[i] == "\\":
        slashes += 1
        i -= 1
    return slashes % 2 == 1

def code_ranges(text: str) -> tuple[SourceSpan, ...]:
    """Only exclude common fences and exact backtick spans; ambiguous cases stay contextual."""
    ranges = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(\x60{3,}|~{3,})([^\n]*)", line.rstrip("\n"))
        if fence is None and match:
            fence = (match[1][0], len(match[1]), offset)
        elif fence is not None and match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
            ranges.append(SourceSpan(fence[2], offset + len(line)))
            fence = None
        offset += len(line)
    if fence is not None:
        ranges.append(SourceSpan(fence[2], len(text)))
    starts = [r.start for r in ranges]
    def covered(a, b):
        i = bisect_right(starts, a) - 1
        return (i >= 0 and ranges[i].end > a) or (i + 1 < len(ranges) and starts[i + 1] < b)
    runs = list(re.finditer(r"\x60+", text))
    next_run, last = {}, {}
    for i in range(len(runs) - 1, -1, -1):
        run = runs[i]
        if covered(run.start(), run.end()):
            continue
        next_run[i] = last.get(len(run[0]))
        last[len(run[0])] = i
    inline_ranges = []
    i = 0
    while i < len(runs):
        opening = runs[i]
        j = next_run.get(i)
        if j is not None and not escaped(text, opening.start()):
            inline_ranges.append(SourceSpan(opening.start(), runs[j].end()))
            i = j + 1
        else:
            i += 1
    ranges.extend(inline_ranges)
    return tuple(sorted(ranges, key=lambda r: r.start))

def build_source_map(snapshot: InputSnapshot, ast: dict, index: tuple[NodeInfo, ...],
                     exact: dict[str, SourceSpan] | None = None) -> tuple[SourceLocation, ...]:
    text, offsets = normalized_newlines(snapshot.text)
    excluded = list(code_ranges(text))
    result = []
    cache = {}

    def occurrences(needle):
        start = 0
        while needle:
            i = text.find(needle, start)
            if i < 0:
                return
            yield i, i + len(needle)
            start = i + len(needle)

    def probe_key(n):
        t, c = n["t"], n.get("c")
        if t == "Math":
            return (t, c[0]["t"], normalized_newlines(c[1])[0])
        if t == "Header":
            return (t, c[0], inline_text(c[2]))
        if t in {"Para", "Plain"}:
            return ("paragraph", inline_text(c))
        if t in {"Code", "CodeBlock", "RawInline", "RawBlock"}:
            return ("code" if t.startswith("Code") else t, normalized_newlines(c[1])[0])
        return None

    keys = [probe_key(at_path(ast, info.path)) for info in index]
    counts = Counter(key for key in keys if key is not None)
    # AST raw fragments are opaque too; do not mistake their text for live Markdown.
    opaque = {key for info, key in zip(index, keys)
              if info.node_type in {"Code", "CodeBlock", "RawBlock", "RawInline"}}
    unresolved = len(opaque) > 2048
    seen_raw = set()
    for info in index:
        if unresolved:
            break
        n = at_path(ast, info.path)
        if n["t"] in {"RawBlock", "RawInline"}:
            literal = normalized_newlines(n["c"][1])[0]
            if literal in seen_raw:
                continue
            seen_raw.add(literal)
            matches = list(occurrences(literal))
            if not matches and n["c"][1]:
                unresolved = True
            excluded.extend(SourceSpan(a, b) for a, b in matches)
    # Merge barriers for O(log n) containment and overlap queries.
    merged = []
    for span in sorted(excluded, key=lambda s: s.start):
        if merged and span.start <= merged[-1].end:
            merged[-1] = SourceSpan(merged[-1].start, max(span.end, merged[-1].end))
        else:
            merged.append(span)
    excluded = merged
    starts = [r.start for r in excluded]
    def intersects(a, b):
        i = bisect_right(starts, a) - 1
        return (i >= 0 and excluded[i].end > a) or (i + 1 < len(starts) and starts[i + 1] < b)
    def contains(a, b):
        i = bisect_right(starts, a) - 1
        return i >= 0 and excluded[i].end >= b
    # Indented/nested code and normalized code spans need not have recoverable
    # offsets. If the lightweight scanner cannot account for them, refuse jumps.
    checked = set()
    for info, key in zip(index, keys):
        if unresolved:
            break
        if info.node_type not in {"Code", "CodeBlock"} or key in checked:
            continue
        checked.add(key)
        literal = key[-1]
        covered = sum(1 for a, b in occurrences(literal) if contains(a, b))
        if not literal or covered < counts[key]:
            unresolved = True

    def find(needle, *, exclude_code=True, unescaped=False):
        if not needle or unresolved:
            return []
        key = (needle, exclude_code, unescaped)
        if key in cache:
            return cache[key]
        if len(cache) >= 2048:
            return []  # Limit optional locating work, never truncate the document.
        found, start = [], 0
        while True:
            i = text.find(needle, start)
            if i < 0:
                cache[key] = found
                return found
            end = i + len(needle)
            start = i + 1
            if exclude_code and intersects(i, end):
                continue
            if unescaped and escaped(text, i):
                continue
            found.append((i, end))
            if len(found) == 2:
                cache[key] = found
                return found  # Two matches already prove ambiguity; never guess order.

    headers = {}
    if not unresolved:
        for match in re.finditer(r"^ {0,3}(#{1,6})[ \t]+([^\n]*)", text, re.M):
            key = (len(match[1]), match[2].strip())
            if not intersects(match.start(), match.end()) and len(headers.get(key, [])) < 2:
                headers.setdefault(key, []).append((match.start(), match.end()))
    for info, key in zip(index, keys):
        n = at_path(ast, info.path)
        t, c = n["t"], n.get("c")
        snippet, candidates = "", []
        confidence = Confidence.UNIQUE_MATCH
        if t == "Header":
            snippet = inline_text(c[2])
            candidates = headers.get((c[0], snippet), [])
            confidence = Confidence.EXACT
        elif t in {"Para", "Plain"}:
            snippet = inline_text(c)
            candidates = find(snippet)
        elif t == "Str":
            snippet = c
        elif t in {"Code", "CodeBlock"}:
            snippet = c[1]
            candidates = find(normalized_newlines(snippet)[0], exclude_code=False)
        elif t in {"RawInline", "RawBlock"}:
            snippet = c[1]
            candidates = find(normalized_newlines(snippet)[0])
        elif t in {"Image", "Link"}:
            snippet = inline_text(c[1]) + " → " + c[2][0]
        elif t == "Math":
            snippet = c[1]
            source = normalized_newlines(c[1])[0]
            pairs = [("$", "$"), ("\\(", "\\)")] if c[0]["t"] == "InlineMath" else [
                ("$$", "$$"), ("\\[", "\\]")]
            if source:
                for left, right in pairs:
                    for a, b in find(left + source + right, unescaped=True):
                        if left == "$" and ((a > 0 and text[a - 1] == "$") or (b < len(text) and text[b] == "$")):
                            continue
                        candidates.append((a, b))
            confidence = Confidence.EXACT
        else:
            snippet = t
        if unresolved or key is not None and counts[key] > 1:
            candidates = []
        span = (exact or {}).get(info.node_id)
        if span is not None:
            confidence = Confidence.EXACT
        elif len(set(candidates)) == 1:
            a, b = candidates[0]
            span = SourceSpan(offsets[a], offsets[b])
        else:
            confidence = Confidence.CONTEXTUAL
        result.append(SourceLocation(
            info.node_id, confidence, span, info.section, info.ordinal,
            snippet[:160], snapshot.session_id, snapshot.revision))
    return tuple(result)
