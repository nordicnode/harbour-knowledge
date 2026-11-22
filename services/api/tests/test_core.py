from app.ingest.chunker import chunk_markdown
from app.ingest.pipeline import normalise, quote_in_text, redact
from app.retrieval.answer import normalise_question


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


def test_redaction_removes_email_addresses() -> None:
    cleaned, counts = redact("Contact maya@harbour.example for help.")

    assert "maya@harbour.example" not in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
    assert counts["REDACTED_EMAIL"] == 1


def test_normalise_drops_quoted_reply() -> None:
    assert normalise("Hello\n> old message\nWorld") == "Hello\nWorld"


def test_question_normalisation_is_stable() -> None:
    assert normalise_question("  What's the REFUND window?  ") == "what's the refund window?"
