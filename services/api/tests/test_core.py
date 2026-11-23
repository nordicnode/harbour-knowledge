import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import EMBEDDING_PIPELINE_REVISION, Settings
from app.errors import AppError
from app.intelligence.base import EvidencePassage, GenerationResult, RewriteResult
from app.intelligence.dev import DevIntelligence, match_tokens
from app.intelligence.openai_compat import OpenAICompatIntelligence
from app.intelligence.local_embeddings import EmbeddingInputTooLong, LocalEmbeddingIntelligence
from app.ingest.chunker import chunk_markdown, split_oversized_chunk
from app.ingest.pipeline import normalise, quote_in_text, redact
from app.ingest.parsers import UPLOAD_FORMATS, parse_bytes, upload_format
from app.retrieval.answer import AskRequest, _supporting_quote, _table_fact_fallback, ask, normalise_question
from app.retrieval.search import RetrievedPassage, SearchOutcome, has_detectable_contradiction, lexical_faithfulness
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


def test_oversized_link_list_with_separator_splits_at_items() -> None:
    text = "\n".join(
        f"- Item {index}: see https://example.com/reference-{index}" for index in range(6)
    ) + "\n---"

    parts = split_oversized_chunk(text)

    assert len(parts) == 2
    assert all(part.strip().startswith("- ") for part in parts)
    assert all(part.strip() for part in parts)


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


def test_short_supported_answer_and_quote_are_not_discarded() -> None:
    text = "Mikey is 40 years old. Mikey was born on August 8th, 1986."
    passage = SimpleNamespace(parent_text=text, child_text=text)
    assert lexical_faithfulness("Mikey is 40 years old. [P:123]", [passage]) == 1.0
    assert lexical_faithfulness("**Mikey is 40 years old.**", [passage]) == 1.0
    assert _supporting_quote("Mikey is 40 years old.", text) == "Mikey is 40 years old."
    assert _supporting_quote("Mikey is 40 years old. Mikey was born on August 8th, 1986.", text) == text


def test_short_answer_with_wrong_age_or_unrelated_fact_is_not_verified() -> None:
    text = "Mikey is 40 years old. Mikey was born on August 8th, 1986."
    passage = SimpleNamespace(parent_text=text, child_text=text)
    assert lexical_faithfulness("Mikey is 41 years old.", [passage]) == 0.0
    assert lexical_faithfulness("Mikey lives in London.", [passage]) == 0.0


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        ("Annual plans are eligible for refunds within 30 days.", "Annual plans are not eligible for refunds within 30 days."),
        ("Annual plans are not eligible for refunds within 30 days.", "Annual plans are eligible for refunds within 30 days."),
        ("Annual plans are refundable.", "Annual plans are non-refundable."),
        ("Team SSO is enabled.", "Team SSO is disabled."),
        ("Team customers can request refunds.", "Team customers can't request refunds."),
        ("Mikey is 40 years old.", "Mikey is 41 years old. Another person is 40 years old."),
        ("Refunds are available.", "Refunds are available only to Enterprise customers."),
        ("Annual plans are eligible. Annual plans are not refundable.", "Annual plans are eligible. Annual plans are refundable."),
    ],
)
def test_grounding_rejects_contradictions_or_omitted_conditions(answer, evidence) -> None:
    passage = SimpleNamespace(child_text=evidence, parent_text=evidence)
    assert lexical_faithfulness(answer, [passage]) < 1.0


def test_grounding_accepts_exact_positive_and_negative_claims() -> None:
    evidence = "Annual plans are not refundable after 30 days. Team plans are refundable within 14 days."
    passage = SimpleNamespace(child_text=evidence, parent_text=evidence)
    assert lexical_faithfulness("Annual plans are not refundable after 30 days.", [passage]) == 1.0
    assert lexical_faithfulness("Team plans are refundable within 14 days.", [passage]) == 1.0
    assert not has_detectable_contradiction("Team plans are refundable within 14 days.", [passage])


def test_grounding_rejects_conflicting_evidence_even_if_one_source_is_cited() -> None:
    cited = SimpleNamespace(child_text="Annual plans are refundable within 30 days.")
    conflict = SimpleNamespace(child_text="Annual plans are not refundable within 30 days.")
    different_number = SimpleNamespace(child_text="Annual plans are refundable within 14 days.")
    claim = "Annual plans are refundable within 30 days."
    assert lexical_faithfulness(claim, [cited], [cited, conflict]) == 0.0
    assert lexical_faithfulness(claim, [cited], [cited, different_number]) == 0.0
    assert has_detectable_contradiction(claim, [cited, conflict])
    assert has_detectable_contradiction(claim, [cited, different_number])
    affirmative = "Team customers can request refunds."
    contraction = SimpleNamespace(child_text="Team customers can't request refunds.")
    assert has_detectable_contradiction(affirmative, [contraction])


def test_grounding_requires_cited_child_not_uncited_parent_or_partial_claims() -> None:
    passage = SimpleNamespace(child_text="Mikey was born in August.", parent_text="Mikey is 40 years old. Mikey was born in August.")
    assert lexical_faithfulness("Mikey is 40 years old.", [passage]) == 0.0
    passage.child_text = "Mikey is 40 years old."
    assert lexical_faithfulness("Mikey is 40 years old. Mikey lives in London.", [passage]) == 0.5


def test_matching_strips_only_trailing_punctuation() -> None:
    assert match_tokens("How old is Mikey?") == ["old", "mikey"]
    assert match_tokens("Mikey is 40 years old.") == ["mikey", "40", "years", "old"]
    assert match_tokens("Error E-4021.") == ["error", "e-4021"]


async def test_dev_generator_abstains_when_question_facts_are_missing() -> None:
    intelligence = DevIntelligence()
    evidence = [EvidencePassage(evidence_id="p1", text="Mikey is 40 years old. Mikey was born on August 8th, 1986.", title="Facts")]
    answer = await intelligence.generate_answer("How old is Mikey?", evidence)
    assert not answer.abstain
    assert "Mikey is 40 years old." in answer.answer_md
    assert answer.cited_ids == ["p1"]
    assert "[2]" not in answer.answer_md
    missing = await intelligence.generate_answer("What city was Mikey born in?", evidence)
    assert missing.abstain


async def test_local_model_citations_use_request_local_labels() -> None:
    provider = object.__new__(OpenAICompatIntelligence)
    provider._chat_json = AsyncMock(return_value={
        "answer_md": "Mikey is 40 years old. [P:E1]", "cited_ids": ["E1", "E9"], "abstain": False,
    })
    evidence = [EvidencePassage(evidence_id=str(uuid4()), text="Mikey is 40 years old.", title="Facts")]
    result = await provider.generate_answer("How old is Mikey?", evidence)
    assert result.cited_ids == [evidence[0].evidence_id]
    assert f"[P:{evidence[0].evidence_id}]" in result.answer_md
    assert "[P:E1]" in provider._chat_json.await_args.args[-1]


def test_table_fact_fallback_requires_model_and_unambiguous_matching_values() -> None:
    document = uuid4()
    table = "| Attribute | Specification |\n| :--- | :--- |\n| **Product Name** | Intel Core i5-9400 |\n| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    passage = SimpleNamespace(passage_id=uuid4(), document_id=document,
                              document_title="Intel Core i5-9400 specifications", child_text=table)
    result = _table_fact_fallback("How many cores does the i5 9400 CPU have?", [passage])
    assert result is not None
    assert result.answer_md == "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    assert result.cited_ids == [str(passage.passage_id)]
    assert lexical_faithfulness(result.answer_md, [passage]) == 1.0
    assert _table_fact_fallback("How many cores does the i5 10400 have?", [passage]) is None
    assert _table_fact_fallback("How many cores does the i5 9400F have?", [passage]) is None
    assert _table_fact_fallback("How many cores does it have?", [passage]) is None
    assert _table_fact_fallback("How many cores are disabled on the i5 9400?", [passage]) is None
    other = SimpleNamespace(passage_id=uuid4(), document_id=uuid4(),
                            document_title=passage.document_title, child_text=table.replace("6 / 6", "4 / 4"))
    assert _table_fact_fallback("How many cores does the i5 9400 have?", [passage, other]) is None
    assert _table_fact_fallback("How many cores does the i5 9400 have?", [other, passage]) is None
    bad_title = SimpleNamespace(passage_id=uuid4(), document_id=uuid4(),
                                document_title="Intel Core i5-10400", child_text=table)
    assert _table_fact_fallback("How many cores does the i5 9400 have?", [bad_title]) is None


async def test_ask_uses_verified_table_row_when_model_omits_citation(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    text = "| Attribute | Specification |\n| :--- | :--- |\n| **Product Name** | Intel Core i5-9400 |\n| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    passage = RetrievedPassage(
        passage_id=uuid4(), parent_id=None, child_text=text, parent_text=text, section_path="Specs",
        page_ref=None, document_id=uuid4(), document_title="Intel Core i5-9400", document_version=1,
        doc_type="faq", effective_from=None, plan_scope=[], version_scope=[], source_id=uuid4(),
        source_title="Specs", source_tier="official_doc", visibility="internal", rerank_score=0.8, coverage=1.0,
    )
    intelligence = SimpleNamespace(
        name="test", rewrite=AsyncMock(return_value=RewriteResult("How many cores does the i5 9400 have?")),
        embed=AsyncMock(return_value=[[1.0]]), generate_answer=AsyncMock(return_value=GenerationResult("6", [])),
    )
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", AsyncMock(return_value=SearchOutcome([passage], 1, 1.0)))
    monkeypatch.setattr(answer_module, "_canonical_match", AsyncMock(return_value=(None, 0.0)))
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[]))
    monkeypatch.setattr(answer_module, "_maybe_propose_fallback", AsyncMock())
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="test"))
    settings = SimpleNamespace(internal_low_threshold=0.34, internal_high_threshold=0.52,
                               public_low_threshold=0.55, public_high_threshold=0.72)
    result = await ask(None, None, settings, AskRequest("How many cores does the i5 9400 have?"))
    assert result["mode"] == "fallback"
    assert "Cores / Threads" in result["answer_md"]
    assert result["citations"][0]["quote"] == "| **Cores / Threads** | 6 / 6 (No Hyper-Threading) |"
    assert result["citations"][0]["verified"]


async def test_ask_accepts_short_grounded_answer_and_rejects_bad_retry(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    text = "Mikey is 40 years old. Mikey was born on August 8th, 1986."
    passage = RetrievedPassage(
        passage_id=uuid4(), parent_id=None, child_text=text, parent_text=text,
        section_path="Overview", page_ref=None, document_id=uuid4(), document_title="Facts",
        document_version=1, doc_type="faq", effective_from=None, plan_scope=[], version_scope=[],
        source_id=uuid4(), source_title="Facts", source_tier="official_doc", visibility="internal",
        rerank_score=0.8, coverage=1.0,
    )
    pid = str(passage.passage_id)
    intelligence = SimpleNamespace(
        name="test", rewrite=AsyncMock(return_value=RewriteResult("How old is Mikey?")),
        embed=AsyncMock(return_value=[[1.0]]),
        generate_answer=AsyncMock(return_value=GenerationResult(f"Mikey is 40 years old. [P:{pid}]", [pid])),
    )
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", AsyncMock(return_value=SearchOutcome([passage], 1, 1.0)))
    monkeypatch.setattr(answer_module, "_canonical_match", AsyncMock(return_value=(None, 0.0)))
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[]))
    monkeypatch.setattr(answer_module, "_maybe_propose_fallback", AsyncMock())
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="test"))
    monkeypatch.setattr(answer_module, "_upsert_gap", AsyncMock(return_value=uuid4()))
    settings = SimpleNamespace(internal_low_threshold=0.34, internal_high_threshold=0.52,
                               public_low_threshold=0.55, public_high_threshold=0.72)
    result = await ask(None, None, settings, AskRequest("How old is Mikey?"))
    assert result["mode"] == "fallback"
    assert "Mikey is 40 years old. [1]" in result["answer_md"]
    assert result["citations"][0]["quote"] == "Mikey is 40 years old."

    intelligence.generate_answer.side_effect = [
        GenerationResult("Mikey is 40 years old.", ["not-a-passage"]),
        GenerationResult("Mikey is 40 years old.", [pid]),
    ]
    result = await ask(None, None, settings, AskRequest("How old is Mikey?"))
    assert result["mode"] == "fallback"
    assert result["citations"][0]["passage_id"] == pid

    intelligence.generate_answer.side_effect = [
        GenerationResult("Mikey is 40 years old.", ["not-a-passage"]),
        GenerationResult("Mikey is 40 years old.", ["still-invalid"]),
    ]
    result = await ask(None, None, settings, AskRequest("How old is Mikey?"))
    assert result["abstained"]

    intelligence.generate_answer.side_effect = [
        GenerationResult("Mikey is 41 years old.", [pid]),
        GenerationResult("Mikey is 42 years old.", [pid], abstain=True),
    ]
    result = await ask(None, None, settings, AskRequest("How old is Mikey?"))
    assert result["abstained"]
    assert result["abstention_reason"] == "The generated answer did not verify against its evidence."

    intelligence.generate_answer.side_effect = [
        GenerationResult("Mikey is 40 years old. Mikey lives in London.", [pid]),
        GenerationResult("Mikey is 40 years old. Mikey lives in London.", [pid]),
    ]
    result = await ask(None, None, settings, AskRequest("How old is Mikey?"))
    assert result["abstained"]
    assert result["abstention_reason"] == "The generated answer did not verify against its evidence."

    intelligence.generate_answer.side_effect = None
    intelligence.generate_answer.return_value = GenerationResult("Mikey is 40 years old.", [pid])
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[{
        "notice": "Two sources disagree.", "public": False,
    }]))
    result = await ask(None, None, settings, AskRequest("How old is Mikey?"))
    assert result["abstained"]
    assert result["abstention_reason"] == "Cited sources have an unresolved conflict and need review."


async def test_canonical_answer_abstains_when_its_evidence_contradicts_it(monkeypatch) -> None:
    import app.retrieval.answer as answer_module

    passage = RetrievedPassage(
        passage_id=uuid4(), parent_id=None, child_text="Annual plans are not refundable.",
        parent_text="Annual plans are not refundable.", section_path="Overview", page_ref=None,
        document_id=uuid4(), document_title="Policy", document_version=1, doc_type="policy",
        effective_from=None, plan_scope=[], version_scope=[], source_id=uuid4(), source_title="Policy",
        source_tier="official_doc", visibility="public", rerank_score=0.9, coverage=1.0,
    )
    canonical = {"id": uuid4(), "question": "Are annual plans refundable?",
                 "answer_md": "Annual plans are refundable.", "current_version": 1}
    citation = answer_module.Citation("e", str(passage.passage_id), passage.child_text,
                                      "Policy", None, "Overview", True)
    intelligence = SimpleNamespace(name="test", rewrite=AsyncMock(return_value=RewriteResult(canonical["question"])),
                                   embed=AsyncMock(return_value=[[1.0]]))
    monkeypatch.setattr(answer_module, "get_intelligence", lambda: intelligence)
    monkeypatch.setattr(answer_module, "hybrid_search", AsyncMock(return_value=SearchOutcome([passage], 1, 1.0)))
    monkeypatch.setattr(answer_module, "_canonical_match", AsyncMock(return_value=(canonical, 1.0)))
    monkeypatch.setattr(answer_module, "_canonical_payload", AsyncMock(return_value=([citation], [passage], False)))
    monkeypatch.setattr(answer_module, "_pending_conflicts", AsyncMock(return_value=[]))
    monkeypatch.setattr(answer_module, "_historical_accuracy", AsyncMock(return_value=0.7))
    monkeypatch.setattr(answer_module, "_log_qa", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(answer_module, "_searched_scope", AsyncMock(return_value="test"))
    monkeypatch.setattr(answer_module, "_upsert_gap", AsyncMock(return_value=uuid4()))
    settings = SimpleNamespace(internal_low_threshold=0.34, internal_high_threshold=0.52,
                               public_low_threshold=0.55, public_high_threshold=0.72)
    for channel in ("public", "web"):
        result = await ask(None, None, settings, AskRequest(canonical["question"], channel=channel))
        assert result["abstained"]
        assert "contradicts" in result["sources_ignored"][0]["reason"]
        assert result["abstention_reason"] == "The approved answer has conflicting evidence and needs review."


def test_redaction_removes_email_addresses() -> None:
    cleaned, counts = redact("Contact maya@harbour.example for help.")

    assert "maya@harbour.example" not in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
    assert counts["REDACTED_EMAIL"] == 1


def test_normalise_preserves_source_content() -> None:
    source = (
        "\ufeff# Eligibility\r\n\r\n> Only annual plans qualify.  \r\n\r\n"
        "Limit:\r\n40\r\n\r\nOn January 1, 2025 wrote:\r\n"
        "--- Original Message ---\r\n\r\n"
        "| Plan | Limit |\r\n| --- | --- |\r\n| Annual | 40 |\r\n\r\n"
        "```python\r\n# configuration\r\n\r\n\r\n    limit = 40  \r\n```\r\n"
    )
    normalized = normalise(source)
    assert normalized == source.removeprefix("\ufeff").replace("\r\n", "\n")
    chunks = chunk_markdown(normalized)
    child_text = "\n".join(chunk.text for chunk in chunks.chunks)
    assert "> Only annual plans qualify." in child_text
    assert "Limit:\n40" in child_text
    assert "| Annual | 40 |" in child_text
    assert "```python\n# configuration\n\n\n    limit = 40  \n```" in child_text
    assert any(chunk.section_path == "Eligibility" and "configuration" in chunk.text for chunk in chunks.chunks)


def test_split_oversized_chunk_preserves_prose_and_table_header() -> None:
    text = "Alpha condition applies only to annual plans. The second condition applies to refunds."
    parts = split_oversized_chunk(text)
    assert "".join(parts) == text
    with pytest.raises(AppError, match="cannot be split safely"):
        split_oversized_chunk("long unpunctuated condition " * 100)
    table = "| Model | Cores |\n| --- | --- |\n| First | 4 |\n| Second | 6 |\n| Third | 8 |"
    parts = split_oversized_chunk(table)
    assert all(part.startswith("| Model | Cores |\n| --- | --- |\n") for part in parts)
    assert all(sum(row in part for part in parts) == 1 for row in ("| First | 4 |", "| Second | 6 |", "| Third | 8 |"))
    with pytest.raises(AppError, match="table row"):
        split_oversized_chunk("| A | B |\n| --- | --- |\n| long | row |")
    with pytest.raises(AppError, match="code block"):
        split_oversized_chunk("```\nexample\n```")


async def test_local_embedder_checks_exact_input_before_encoding() -> None:
    class Vector:
        def tolist(self):
            return [1.0]

    class Model:
        max_seq_length = 12
        default_prompt_name = "doc"
        prompts = {"doc": "AB"}
        encoded = False

        def preprocess(self, inputs, prompt, processing_kwargs):
            assert processing_kwargs["text"]["truncation"] is False
            # One token per character, plus start/end special tokens.
            return {"input_ids": [[0] * (len(prompt + text) + 2) for text in inputs]}

        def encode(self, inputs, **kwargs):
            self.encoded = True
            return [Vector() for _ in inputs]

    settings = SimpleNamespace(local_embedding_batch_size=2, local_embedding_query_instruction="")
    provider = LocalEmbeddingIntelligence(settings)
    provider._model = model = Model()
    assert await provider.embed(["12345678"]) == [[1.0]]
    model.encoded = False
    with pytest.raises(EmbeddingInputTooLong) as error:
        await provider.embed(["123456789"])
    assert (error.value.index, error.value.tokens, error.value.limit) == (0, 13, 12)
    assert not model.encoded
    settings.local_embedding_query_instruction = "I"
    with pytest.raises(EmbeddingInputTooLong):
        await provider.embed(["x"], task="query")
    assert not model.encoded


def test_chunk_preserves_literal_html_and_long_fenced_code() -> None:
    code = "```text\n<!-- page: 17 -->\n# Not a heading\n\n" + "value = 40\n" * 700 + "```"
    result = chunk_markdown("# Example\n\n" + code, max_section_tokens=5)
    assert len(result.chunks) == 1
    assert result.chunks[0].text == code
    assert result.chunks[0].section_path == "Example"


def _pdf_with_text(*page_texts: str) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_font("helvetica", size=12)
    for text in page_texts:
        pdf.add_page()
        if text:
            pdf.cell(text=text)
    return bytes(pdf.output())


def test_pdf_rejects_blank_pages_not_markers() -> None:
    with pytest.raises(AppError, match="no extractable text layer"):
        parse_bytes("blank.pdf", "application/pdf", _pdf_with_text("", "", ""), "Blank")


def test_pdf_preserves_short_text_and_reports_pages_without_text() -> None:
    parsed = parse_bytes("mixed.pdf", "application/pdf", _pdf_with_text("42", "", "Second page."), "Mixed")
    assert parsed.page_count == 3
    assert parsed.metadata["pages_without_extractable_text"] == [2]
    chunks = chunk_markdown(normalise(parsed.markdown), page_markers=True)
    first = next(chunk for chunk in chunks.chunks if "42" in chunk.text)
    last = next(chunk for chunk in chunks.chunks if "Second page." in chunk.text)
    assert first.page_ref == chunks.sections[first.section_index].page_ref == "1"
    assert last.page_ref == chunks.sections[last.section_index].page_ref == "3"
    assert not any(chunk.page_ref == "2" for chunk in chunks.chunks)


def test_page_boundaries_preserve_headings_and_chunk_references() -> None:
    markdown = (
        "<!-- page: 1 -->\n\n# Refunds\n\nPage one rule.\n\n"
        "<!-- page: 2 -->\n\nPage two exception.\n\n"
        "<!-- page: 3 -->\n\n## Limits\n\nPage three limit."
    )
    result = chunk_markdown(markdown, page_markers=True, max_section_tokens=4)
    assert [(chunk.text, chunk.page_ref, chunk.section_path) for chunk in result.chunks] == [
        ("Page one rule.", "1", "Refunds"),
        ("Page two exception.", "2", "Refunds"),
        ("Page three limit.", "3", "Refunds › Limits"),
    ]
    assert all(result.sections[chunk.section_index].page_ref == chunk.page_ref for chunk in result.chunks)


def test_page_markers_inside_non_pdf_code_are_literal() -> None:
    markdown = "```html\n<!-- page: 17 -->\n# Not a heading\n```"
    result = chunk_markdown(markdown)
    assert len(result.chunks) == 1
    assert result.chunks[0].text == markdown
    assert result.chunks[0].page_ref is None


def _docx_bytes(document) -> bytes:
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_docx_interleaves_paragraphs_and_tables_under_their_headings() -> None:
    from docx import Document
    from docx.oxml import OxmlElement

    document = Document()
    document.add_heading("First rules", level=1)
    document.add_paragraph("First introduction.")
    table = document.add_table(rows=2, cols=2)
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    table.cell(0, 0).text, table.cell(0, 1).text = "Plan", "Limit"
    table.cell(1, 0).text, table.cell(1, 1).text = "Annual", "40"
    document.add_paragraph("After the first table.")
    document.add_heading("Second rules", level=1)
    second = document.add_table(rows=1, cols=2)
    second.cell(0, 0).text, second.cell(0, 1).text = "Monthly", "10"
    parsed = parse_bytes("rules.docx", None, _docx_bytes(document), "Rules")
    assert parsed.markdown.index("First introduction.") < parsed.markdown.index("| Annual | 40 |")
    assert parsed.markdown.index("| Annual | 40 |") < parsed.markdown.index("After the first table.")
    assert parsed.markdown.index("After the first table.") < parsed.markdown.index("# Second rules") < parsed.markdown.index("| Monthly | 10 |")
    assert "| Plan | Limit |\n| --- | --- |\n| Annual | 40 |" in parsed.markdown
    assert "|  |  |\n| --- | --- |\n| Monthly | 10 |" in parsed.markdown
    chunks = chunk_markdown(parsed.markdown)
    assert next(chunk for chunk in chunks.chunks if "| Annual | 40 |" in chunk.text).section_path == "First rules"
    assert next(chunk for chunk in chunks.chunks if "| Monthly | 10 |" in chunk.text).section_path == "Second rules"


def test_docx_rejects_irregular_rows_instead_of_shifting_cells() -> None:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Annual"
    grid_before = OxmlElement("w:gridBefore")
    grid_before.set(qn("w:val"), "1")
    table.rows[0]._tr.get_or_add_trPr().append(grid_before)
    with pytest.raises(AppError, match="Irregular DOCX table rows"):
        parse_bytes("irregular.docx", None, _docx_bytes(document), "Irregular")


def test_docx_rejects_nested_tables_instead_of_losing_content() -> None:
    from docx import Document
    document = Document()
    document.add_table(rows=1, cols=1).cell(0, 0).add_table(rows=1, cols=1)
    with pytest.raises(AppError, match="Nested DOCX tables"):
        parse_bytes("nested.docx", None, _docx_bytes(document), "Nested")


def test_html_nested_blocks_are_emitted_once_and_keep_order() -> None:
    html = b'''<html><head><title>Rules</title></head><body><article>
      Intro <b>inline</b> text <em>with <strong>emphasis</strong></em>.
      <h1>Rules</h1><ul><li><p>Only annual plans qualify.</p><ul>
      <li><p>Apply within 30 days.</p></li><li>Keep receipt.</li></ul></li></ul>
      <blockquote><p>Special condition.</p><p>Must be verified.</p></blockquote>
      <table><caption>Limits</caption><tr><th>Plan</th><th>Limit</th></tr>
      <tr><td><p>Annual</p></td><td><p>40</p></td></tr></table>
      <p>Final paragraph.</p></article></body></html>'''
    parsed = parse_bytes("rules.html", None, html, "Rules")
    assert parsed.title == "Rules"
    for fact in ("Intro inline text with emphasis.", "Only annual plans qualify.", "Apply within 30 days.",
                 "Keep receipt.", "Special condition.", "Must be verified.", "Annual", "Final paragraph."):
        assert parsed.markdown.count(fact) == 1
    assert "- Only annual plans qualify.\n  - Apply within 30 days.\n  - Keep receipt." in parsed.markdown
    assert "> Special condition.\n>\n> Must be verified." in parsed.markdown
    assert "Limits\n\n| Plan | Limit |\n| --- | --- |\n| Annual | 40 |" in parsed.markdown
    assert parsed.markdown.index("Only annual") < parsed.markdown.index("Special condition") < parsed.markdown.index("Annual | 40") < parsed.markdown.index("Final paragraph")


def test_html_tables_preserve_spans_and_do_not_invent_headers() -> None:
    html = b'''<table><tr><td rowspan="2">Annual</td><td>US</td><td>40</td></tr>
    <tr><td>EU</td><td>35</td></tr><tr><td colspan="2">Other</td><td>10</td></tr></table>'''
    parsed = parse_bytes("table.html", None, html, "Table")
    assert parsed.markdown == (
        "|  |  |  |\n| --- | --- | --- |\n| Annual | US | 40 |\n"
        "| Annual | EU | 35 |\n| Other | Other | 10 |"
    )
    for bad in (b'<table><tr><td rowspan="2">A</td></tr></table>',
                b'<table><tr><td><table><tr><td>A</td></tr></table></td></tr></table>',
                b'<table><tr><td rowspan="2">A</td></tr><tr></tr></table>'):
        with pytest.raises(AppError, match="HTML table"):
            parse_bytes("bad.html", None, bad, "Bad")


def test_html_preserves_ordered_list_values_and_preformatted_code() -> None:
    html = b'<p>Line one<br>Line two</p><ol start="3"><li>Third</li><li value="8">Eighth</li></ol><pre>before\n```\n# literal\n```\nafter</pre>'
    parsed = parse_bytes("code.html", None, html, "Code")
    assert "Line one\nLine two" in parsed.markdown
    assert "3. Third\n8. Eighth" in parsed.markdown
    assert "````\nbefore\n```\n# literal\n```\nafter\n````" in parsed.markdown
    assert any("# literal" in chunk.text and chunk.section_path == "Overview" for chunk in chunk_markdown(parsed.markdown).chunks)
    reversed_list = parse_bytes("list.html", None, b'<ol reversed><li>Three</li><li value="7">Seven</li><li>Six</li></ol>', "List")
    assert reversed_list.markdown == "3. Three\n7. Seven\n6. Six"


def test_empty_docx_and_html_are_rejected() -> None:
    from docx import Document
    with pytest.raises(AppError, match="no extractable text"):
        parse_bytes("empty.docx", None, _docx_bytes(Document()), "Empty")
    with pytest.raises(AppError, match="no readable text"):
        parse_bytes("empty.html", None, b"<html><head><title>Title</title></head><body></body></html>", "Empty")


def test_question_normalisation_is_stable() -> None:
    assert normalise_question("  What's the REFUND window?  ") == "what's the refund window?"


@pytest.mark.parametrize("extension,kind", sorted(UPLOAD_FORMATS.items()))
def test_supported_upload_extensions_choose_matching_parser(extension, kind) -> None:
    from docx import Document
    from pptx import Presentation

    if kind == "pdf":
        data, parser = _pdf_with_text("A useful fact."), "pypdf"
    elif kind == "docx":
        document = Document()
        document.add_paragraph("A useful fact.")
        data, parser = _docx_bytes(document), "python-docx"
    elif kind == "pptx":
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "A useful fact."
        output = io.BytesIO()
        presentation.save(output)
        data, parser = output.getvalue(), "python-pptx"
    elif kind == "html":
        data, parser = b"<p>A useful fact.</p>", "beautifulsoup"
    else:
        data, parser = b"A useful fact.", "text"
    filename = "reference" + extension.upper()
    assert upload_format(filename) == kind
    parsed = parse_bytes(filename, "application/pdf", data, "Reference", kind=kind)
    assert parsed.parser == parser and "A useful fact." in parsed.markdown


@pytest.mark.parametrize("filename", ["file.xlsx", "file.epub", "file.png", "file.jpg", "file.jpeg", "file.webp", "file.exe", "file"])
def test_unsupported_upload_extensions_rejected(filename) -> None:
    with pytest.raises(AppError, match="Unsupported file type"):
        upload_format(filename)


async def test_unsupported_upload_rejected_at_http_boundary(monkeypatch) -> None:
    import httpx
    from app.deps import get_context
    from app.main import app
    from app.routers import sources

    read_upload = AsyncMock()
    write_original = AsyncMock()
    monkeypatch.setattr(sources, "_read_upload", read_upload)
    monkeypatch.setattr(sources, "save_bytes", write_original)
    app.dependency_overrides[get_context] = lambda: SimpleNamespace(
        session=SimpleNamespace(role="admin"), settings=SimpleNamespace(max_upload_bytes=1024), conn=None,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for filename in ("sheet.xlsx", "photo.png", "book.epub", "report.exe"):
                response = await client.post("/v1/sources", files={"file": (filename, b"payload", "text/plain")})
                assert response.status_code == 422
                assert "Unsupported file type" in response.json()["error"]["message"]
        read_upload.assert_not_awaited()
        write_original.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_context, None)


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


def test_embedding_identity_is_stable_and_config_sensitive() -> None:
    def settings(**overrides) -> Settings:
        values = {
            "embedding_provider": "dev",
            "embedding_model": "dev-hashed-v1",
            "embedding_dim": 1024,
            "embedding_version": "1",
        }
        values.update(overrides)
        return Settings(**values)

    base = settings()
    assert base.embedding_identity.startswith(f"p{EMBEDDING_PIPELINE_REVISION}-")
    assert base.embedding_identity == settings().embedding_identity
    assert base.embedding_identity != settings(embedding_version="2").embedding_identity
    assert base.embedding_identity != settings(embedding_model="another-model").embedding_identity
    assert base.embedding_identity != settings(embedding_provider="openai", model_embed="text-embedding-3-small").embedding_identity

    # The query instruction changes the query vector space for local models.
    first = settings(embedding_provider="local", local_embedding_model="model-a", local_embedding_query_instruction="retrieve a")
    second = settings(embedding_provider="local", local_embedding_model="model-a", local_embedding_query_instruction="retrieve b")
    assert first.embedding_identity != second.embedding_identity
    assert first.embedding_identity != settings(
        embedding_provider="local",
        local_embedding_model="model-a",
        local_embedding_query_instruction="retrieve a",
        local_embedding_max_seq_length=256,
    ).embedding_identity
