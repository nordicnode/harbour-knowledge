from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
PAGE_RE = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'(\[])")


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass
class Section:
    text: str
    section_path: str
    page_ref: str | None
    token_count: int


@dataclass
class Chunk:
    text: str
    section_index: int
    section_path: str
    page_ref: str | None
    token_count: int
    chunk_index: int


@dataclass
class ChunkSet:
    sections: list[Section] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)


def _is_table_block(lines: list[str]) -> bool:
    return bool(lines) and all(line.strip().startswith("|") for line in lines if line.strip())


def _is_step_block(lines: list[str]) -> bool:
    # A trailing horizontal rule is a separator, not a prose sentence. Ignore
    # it so link-heavy lists can still split at their item boundaries.
    meaningful = [line for line in lines if line.strip() and not RULE_RE.match(line)]
    return bool(meaningful) and all(re.match(r"^\s*(\d+[.)]|[-*])\s", line) for line in meaningful)


def _fence_transition(line: str, fence: str | None) -> str | None:
    match = FENCE_RE.match(line)
    if not match:
        return fence
    marker, suffix = match.groups()
    if fence is None:
        return marker
    if marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
        return None
    return fence


def _split_blocks(text: str) -> list[tuple[list[str], str | None]]:
    """Paragraph-aware blocks that keep tables, lists, and steps intact."""
    blocks: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        nonlocal current
        cleaned = list(current)
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            blocks.append((cleaned, None))
        current = []

    for raw in text.splitlines():
        next_fence = _fence_transition(raw, fence)
        if fence is not None:
            current.append(raw)
            fence = next_fence
            if fence is None:
                flush()
            continue
        if next_fence is not None:
            flush()
            current.append(raw)
            fence = next_fence
            continue
        if not raw.strip():
            if current and _is_table_block(current):
                continue
            if current and _is_step_block(current):
                continue
            flush()
            continue
        if current and _is_table_block(current) and not raw.strip().startswith("|"):
            flush()
        current.append(raw)
    flush()
    return blocks


def split_oversized_chunk(text: str) -> list[str]:
    """Split a too-large child without dropping text or inventing evidence.

    Tables repeat only their actual header; splitting a single table row or code
    block would destroy its structure, so those cases fail explicitly.
    """
    from ..errors import AppError

    lines = text.splitlines()
    if lines and FENCE_RE.match(lines[0]):
        raise AppError("ingest_failed", "A code block exceeds the local embedding limit; split it into smaller blocks.")
    if _is_table_block(lines):
        if len(lines) < 3 or not re.fullmatch(r"[|:\-\s]+", lines[1]):
            raise AppError("ingest_failed", "A table exceeds the local embedding limit and has no splittable header.")
        rows = lines[2:]
        if len(rows) < 2:
            raise AppError("ingest_failed", "A table row exceeds the local embedding limit; split the source table.")
        middle = len(rows) // 2
        header = lines[:2]
        return ["\n".join(header + rows[:middle]), "\n".join(header + rows[middle:])]

    # Do not split in the middle of a sentence or identifier: doing so can
    # separate a claim from its negation or exception. Lists split at item
    # boundaries; prose only at sentence boundaries.
    if _is_step_block(lines):
        # Nested list lines belong to their parent item, not standalone steps.
        candidates = [match.end() for match in re.finditer(r"\n(?=(?:\d+[.)]|[-*])\s)", text)
                      if 0 < match.end() < len(text)]
        # Never emit a separator-only passage just because it sits near the
        # midpoint; both sides must still contain list or prose content.
        candidates = [
            position for position in candidates
            if any(line.strip() and not RULE_RE.match(line) for line in text[:position].splitlines())
            and any(line.strip() and not RULE_RE.match(line) for line in text[position:].splitlines())
        ]
    else:
        candidates = [match.end() for match in SENTENCE_SPLIT_RE.finditer(text) if 0 < match.end() < len(text)]
    if not candidates:
        raise AppError(
            "ingest_failed",
            "A passage exceeds the local embedding limit and cannot be split safely; split the source text.",
        )
    midpoint = len(text) // 2
    split = min(candidates, key=lambda position: abs(position - midpoint))
    return [text[:split], text[split:]]


def _split_sentences(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    if estimate_tokens(text) <= target_tokens:
        return [text]
    compact = re.sub(r"\s+", " ", text).strip()
    sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(compact) if s.strip()]
    if len(sentences) <= 1:
        # Preserve an indivisible sentence for the embedder's exact limit
        # check rather than cutting off a condition or a long identifier.
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        tokens = estimate_tokens(sentence)
        if current and current_tokens + tokens > target_tokens:
            chunks.append(" ".join(current))
            carry: list[str] = []
            if overlap_tokens > 0:
                carry_tokens = 0
                for existing in reversed(current):
                    if carry_tokens + estimate_tokens(existing) > overlap_tokens:
                        break
                    carry.insert(0, existing)
                    carry_tokens += estimate_tokens(existing)
            current = carry
            current_tokens = sum(estimate_tokens(s) for s in current)
        current.append(sentence)
        current_tokens += tokens
    if current:
        chunks.append(" ".join(current))
    return chunks


def chunk_markdown(
    markdown: str,
    doc_type: str = "faq",
    max_section_tokens: int = 1500,
    *,
    page_markers: bool = False,
) -> ChunkSet:
    """Heading-aware recursive chunking with parent sections and child chunks.

    Defaults follow the spec: 256-512 token children, section parents under
    ~1,500 tokens, procedures and policies get overlap so a step or condition is
    not orphaned from its rule.
    """
    target = 340 if doc_type in {"policy", "procedure", "pricing"} else 240
    overlap = 60 if doc_type in {"policy", "procedure"} else 20

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    raw_sections: list[tuple[str, str, str | None]] = []
    path_stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_path = "Overview"
    current_page: str | None = None
    fence: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip("\n")
        if body:
            raw_sections.append((body, current_path, current_page))
        current_lines = []

    for line in lines:
        page_match = PAGE_RE.match(line.strip()) if page_markers else None
        if page_match:
            # PDF markers are inserted by the parser, even when extracted text
            # happens to contain an unterminated Markdown fence.
            flush()
            current_page = page_match.group(1)
            fence = None
            continue
        next_fence = _fence_transition(line, fence)
        if fence is not None or next_fence is not None:
            current_lines.append(line)
            fence = next_fence
            continue
        heading = HEADING_RE.match(line.strip())
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            path_stack = [(lvl, name) for lvl, name in path_stack if lvl < level]
            path_stack.append((level, title))
            current_path = " › ".join(name for _, name in path_stack)
            continue
        current_lines.append(line)
    flush()

    if not raw_sections:
        raw_sections = [(markdown, "Overview", None)]

    sections: list[Section] = []
    pending: list[tuple[str, str, str | None]] = []
    for body, path, page in raw_sections:
        if estimate_tokens(body) <= max_section_tokens:
            pending.append((body, path, page))
            continue
        blocks = _split_blocks(body)
        current: list[str] = []
        current_tokens = 0
        part = 1
        for block_lines, block_page in blocks:
            block_text = "\n".join(block_lines)
            tokens = estimate_tokens(block_text)
            if current and current_tokens + tokens > max_section_tokens:
                pending.append(("\n\n".join(current), f"{path} (part {part})", page))
                part += 1
                current = []
                current_tokens = 0
            current.append(block_text)
            current_tokens += tokens
            if block_page:
                page = block_page
        if current:
            pending.append(("\n\n".join(current), f"{path} (part {part})" if part > 1 else path, page))

    chunks: list[Chunk] = []
    chunk_index = 0
    for body, path, page in pending:
        text = body.strip("\n")
        if not text:
            continue
        sections.append(
            Section(
                text=text,
                section_path=path,
                page_ref=page,
                token_count=estimate_tokens(text),
            )
        )
        section_index = len(sections) - 1
        blocks = _split_blocks(text)
        child_texts: list[str] = []
        for block_lines, _block_page in blocks:
            block_text = "\n".join(block_lines).strip("\n")
            if not block_text:
                continue
            if _is_table_block(block_lines) or _is_step_block(block_lines) or FENCE_RE.match(block_lines[0]):
                child_texts.append(block_text)
                continue
            child_texts.extend(_split_sentences(block_text, target, overlap))
        if not child_texts:
            child_texts = [text]
        for child in child_texts:
            child = child.strip("\n")
            if not child.strip():
                continue
            chunks.append(
                Chunk(
                    text=child,
                    section_index=section_index,
                    section_path=path,
                    page_ref=page,
                    token_count=estimate_tokens(child),
                    chunk_index=chunk_index,
                )
            )
            chunk_index += 1

    return ChunkSet(sections=sections, chunks=chunks)
