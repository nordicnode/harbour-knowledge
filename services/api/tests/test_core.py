import pytest

from app.errors import AppError
from app.ingest.chunker import chunk_markdown
from app.ingest.pipeline import normalise, quote_in_text, redact
from app.retrieval.answer import _supporting_quote, normalise_question
from app.routers.sources import _read_upload


def test_chunking_keeps_heading_paths_and_steps() -> None:
    markdown = """# Refunds

## Annual plans

Refunds are available for 30 days from purchase.

1. Open billing.
2. Choose the annual plan.
3. Confirm the cancellation.
"""

    chunk_set = chunk_markdown(markdown, doc_type="policy")

    paths = [section.section_path for section in chunk_set.sections]
    assert "Refunds › Annual plans" in paths
    assert all(chunk.section_index < len(chunk_set.sections) for chunk in chunk_set.chunks)
    assert any("Open billing." in chunk.text for chunk in chunk_set.chunks)


def test_quote_verification_tolerates_line_wrapping() -> None:
    passage = "Refunds are available for 30\ndays from purchase."
    assert quote_in_text("available for 30 days", passage)
    assert not quote_in_text("available for 14 days", passage)


def test_supporting_quote_is_exact_and_relevant() -> None:
    passage = (
        "Annual plans can be refunded within 14 days of purchase. "
        "After the 14-day window, annual plans are non-refundable."
    )
    answer = "Annual plans are non-refundable after the 14-day window."

    quote = _supporting_quote(answer, passage)

    assert quote is not None
    assert "14-day window" in quote
    assert quote_in_text(quote, passage)


def test_supporting_quote_rejects_unrelated_passage() -> None:
    assert _supporting_quote(
        "When will my invoice arrive?",
        "SAML SSO is available on the Enterprise plan.",
    ) is None


def test_redaction_removes_email_addresses() -> None:
    cleaned, counts = redact("Contact maya@harbour.example for help.")

    assert "maya@harbour.example" not in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
    assert counts["REDACTED_EMAIL"] == 1


def test_normalise_drops_quoted_reply() -> None:
    assert normalise("Hello\n> old message\nWorld") == "Hello\nWorld"


def test_question_normalisation_is_stable() -> None:
    assert normalise_question("  What's the REFUND window?  ") == "what's the refund window?"


class _ChunkedUpload:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, size: int = -1) -> bytes:
        return next(self._chunks, b"")


async def test_upload_reader_stops_when_the_limit_is_exceeded() -> None:
    upload = _ChunkedUpload([b"a" * 60, b"b" * 60])

    with pytest.raises(AppError) as caught:
        await _read_upload(upload, 100)

    assert caught.value.code == "validation"
    assert "limited" in caught.value.message


async def test_upload_reader_accepts_a_file_within_the_limit() -> None:
    upload = _ChunkedUpload([b"hello", b" world"])

    assert await _read_upload(upload, 100) == b"hello world"
