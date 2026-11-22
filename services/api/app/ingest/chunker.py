from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
PAGE_RE = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->$")
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
    meaningful = [line for line in lines if line.strip()]
    return bool(meaningful) and all(re.match(r"^\s*(\d+[.)]|[-*])\s", line) for line in meaningful)


def _split_blocks(text: str) -> list[tuple[list[str], str | None]]:
    """Paragraph-aware blocks that keep tables, lists, and steps intact."""
    blocks: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    page: str | None = None

    def flush() -> None:
        nonlocal current
        cleaned = list(current)
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            blocks.append((cleaned, page))
        current = []

    for raw in text.splitlines():
        page_match = PAGE_RE.match(raw.strip())
        if page_match:
            flush()
            page = page_match.group(1)
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


def _split_sentences(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    compact = re.sub(r"\s+", " ", text).strip()
    if estimate_tokens(compact) <= target_tokens:
        return [compact]
    sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(compact) if s.strip()]
    if len(sentences) <= 1:
        # A single long line with no sentence boundary: split on word boundaries only.
        words = compact.split()
        step = max(20, int(target_tokens * 4))
        return [" ".join(words[i:i + step]) for i in range(0, len(words), step)]

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

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            raw_sections.append((body, current_path, current_page))
        current_lines = []

    for line in lines:
        page_match = PAGE_RE.match(line.strip())
        if page_match:
            current_page = page_match.group(1)
            current_lines.append(line)
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
        text = re.sub(r"\n{3,}", "\n\n", body).strip()
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
            block_text = "\n".join(block_lines).strip()
            if not block_text:
                continue
            if _is_table_block(block_lines) or _is_step_block(block_lines):
                child_texts.append(block_text)
                continue
            child_texts.extend(_split_sentences(block_text, target, overlap))
        if not child_texts:
            child_texts = [text]
        for child in child_texts:
            child = child.strip()
            if not child:
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
