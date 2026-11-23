from __future__ import annotations

import re
from typing import Sequence

# A conjunction only starts another question when it is followed by an
# interrogative or an auxiliary verb. "Terms and conditions" is one subject;
# "when was it released and how many cores does it have" is two requests.
_INTERROGATIVE = (
    "what|when|where|why|how|who|whom|which|whose|"
    "does|do|did|is|are|was|were|can|could|should|would|will|"
    "much|many"
)
_COMPOUND_AND_RE = re.compile(
    rf"\s+(?:and|also|plus|as well as)\s+(?=(?:{_INTERROGATIVE})\b)", re.I
)
_INTERROGATIVE_RE = re.compile(rf"\b(?:{_INTERROGATIVE})\b", re.I)
# A full stop or newline can separate two requests just as a question mark can:
# "When was it released. How many cores does it have?" is still two questions.
# Only split when the next sentence starts with an interrogative, so ordinary
# prose that merely mentions a fact afterwards stays one request.
_SENTENCE_SPLIT_RE = re.compile(
    rf"(?<=[.!?])\s+(?=(?:{_INTERROGATIVE})\b)", re.I
)


def _newline_parts(question: str) -> list[str]:
    """Non-empty lines, but only when at least two of them ask something."""
    lines = [line.strip() for line in question.splitlines() if line.strip()]
    if len(lines) > 1 and sum(1 for line in lines if _INTERROGATIVE_RE.search(line)) > 1:
        return lines
    return []


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = " ".join(value.split()).strip()
        key = text.casefold().strip(" ?.!;")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def question_part_count(question: str) -> int:
    """Count explicit requests without guessing at ordinary prose.

    Multiple question marks are decisive. A semicolon separates clauses, and a
    conjunction followed by an interrogative is a second request. Everything
    else remains one question; the rewrite model can still split a subtle case.
    """
    text = " ".join(question.split())
    marks = text.count("?")
    if marks > 1:
        return marks
    if ";" in text:
        clauses = [part for part in text.split(";") if part.strip()]
        if len(clauses) > 1 and any(_INTERROGATIVE_RE.search(clause) for clause in clauses[1:]):
            return len(clauses)
    sentence_parts = _SENTENCE_SPLIT_RE.split(text)
    if len(sentence_parts) > 1:
        return len(sentence_parts)
    if len(_newline_parts(question)) > 1:
        return len(_newline_parts(question))
    if _COMPOUND_AND_RE.search(text):
        return 2
    return 1


def split_question_parts(question: str) -> list[str]:
    """Conservatively split explicit multi-question input.

    This is a safety net for provider failures, not the primary decomposition:
    the rewrite model resolves pronouns and preserves qualifiers. It only splits
    on separators that unambiguously begin a new request.
    """
    text = " ".join(question.split())
    if not text:
        return []

    pieces: list[str]
    if text.count("?") > 1:
        pieces = re.findall(r"[^?]+\?", text)
        tail = text[text.rfind("?") + 1 :].strip()
        if tail:
            pieces.append(tail)
    elif len(_SENTENCE_SPLIT_RE.split(text)) > 1:
        pieces = _SENTENCE_SPLIT_RE.split(text)
    elif len(_newline_parts(question)) > 1:
        pieces = _newline_parts(question)
    elif ";" in text and question_part_count(text) > 1:
        pieces = text.split(";")
    elif _COMPOUND_AND_RE.search(text):
        pieces = _COMPOUND_AND_RE.split(text)
    else:
        pieces = [text]
    return _dedupe(pieces)


def normalise_subquestions(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return _dedupe([str(item) for item in raw])
