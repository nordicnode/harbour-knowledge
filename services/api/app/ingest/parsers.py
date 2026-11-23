from __future__ import annotations

import io
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..errors import validation


@dataclass
class ParsedDocument:
    title: str
    markdown: str
    parser: str
    page_count: int = 0
    metadata: dict = field(default_factory=dict)


UPLOAD_FORMATS = {
    ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx",
    ".html": "html", ".htm": "html", ".txt": "text",
    ".md": "markdown", ".markdown": "markdown", ".csv": "text", ".log": "text",
}
UPLOAD_FORMAT_MESSAGE = (
    "Unsupported file type. Upload a PDF, DOCX, PPTX, HTML, TXT, Markdown, CSV, or LOG file. "
    "XLSX, EPUB, and image uploads are not supported."
)


def upload_format(filename: str) -> str:
    """Select the upload parser by extension, never an untrusted MIME header."""
    kind = UPLOAD_FORMATS.get(Path(filename).suffix.lower())
    if kind is None:
        raise validation(UPLOAD_FORMAT_MESSAGE)
    return kind


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _infer_headings(text: str) -> str:
    """Give plain-text PDFs enough structure to chunk by section.

    Conservative: only short standalone lines without terminal punctuation are
    promoted, so ordinary sentences stay in their paragraph.
    """
    lines = text.splitlines()
    out: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        previous_blank = index == 0 or not lines[index - 1].strip()
        next_blank = index + 1 >= len(lines) or not lines[index + 1].strip()
        looks_heading = (
            stripped
            and len(stripped) <= 70
            and len(stripped.split()) <= 8
            and previous_blank
            and next_blank
            and not re.search(r"[.!?,;:]$", stripped)
            and not stripped.startswith(("-", "*", "•", "|", ">"))
            and not re.match(r"^\d+[.)]\s", stripped)
            and not re.match(r"^<!--", stripped)
            and not stripped.isupper()
            and stripped[0].isupper()
            and not stripped.endswith(":")
        )
        if stripped.isupper() and len(stripped) <= 60 and previous_blank and next_blank:
            looks_heading = True
        out.append(("## " + stripped.title()) if looks_heading else line)
    return "\n".join(out)


def _parse_pdf(data: bytes, title: str) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    pages_without_text: list[int] = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            pages_without_text.append(number)
        pages.append(f"<!-- page: {number} -->\n\n{text}")
    if len(pages_without_text) == len(pages):
        raise validation("The PDF has no extractable text layer. It needs OCR before it can be ingested.")
    markdown = _infer_headings("\n\n".join(pages))
    return ParsedDocument(
        title=title,
        markdown=markdown,
        parser="pypdf",
        page_count=len(reader.pages),
        # Blank pages may be intentional or scanned: record what was observed,
        # without claiming to distinguish the two or having performed OCR.
        metadata={"page_count": len(reader.pages), "pages_without_extractable_text": pages_without_text},
    )


def _markdown_table(rows: list[list[str]], *, has_header: bool) -> str:
    """Render every source row; a blank Markdown header is not a source row."""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    if not has_header:
        padded.insert(0, [""] * width)
    separator = ["---"] * width
    padded.insert(1, separator)
    return "\n".join("| " + " | ".join(row) + " |" for row in padded)


def _parse_docx(data: bytes, title: str) -> ParsedDocument:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    blocks: list[str] = []
    # Paragraphs and tables must be walked together in document order; separate
    # collections move tables to the end and can assign them to the wrong heading.
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            style = (item.style.name or "").lower()
            if style.startswith("heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "2"
                blocks.append(f"{'#' * min(int(level), 4)} {text}")
            elif style.startswith("list"):
                blocks.append(f"- {text}")
            else:
                blocks.append(text)
        elif isinstance(item, Table):
            # python-docx omits leading/trailing grid slots from row.cells;
            # padding only at the end would silently attach values to the wrong columns.
            for row in item.rows:
                properties = row._tr.trPr
                if properties is not None and any(
                    (element := properties.find(qn(f"w:{name}"))) is not None
                    and element.get(qn("w:val"), "0") != "0"
                    for name in ("gridBefore", "gridAfter")
                ):
                    raise validation("Irregular DOCX table rows cannot be extracted reliably.")
            if any(cell.tables for row in item.rows for cell in row.cells):
                raise validation("Nested DOCX tables cannot be extracted reliably.")
            rows = [
                [re.sub(r"\s*\n\s*", "<br>", cell.text.strip()).replace("|", "\\|")
                 for cell in row.cells]
                for row in item.rows
            ]
            if rows and any(cell for row in rows for cell in row):
                tr_pr = item.rows[0]._tr.trPr
                header_flag = tr_pr.find(qn("w:tblHeader")) if tr_pr is not None else None
                has_header = header_flag is not None and header_flag.get(qn("w:val"), "true") not in {"0", "false", "off"}
                blocks.append(_markdown_table(rows, has_header=has_header))
    markdown = "\n\n".join(blocks)
    if not markdown.strip():
        raise validation("The document contains no extractable text.")
    return ParsedDocument(title=title, markdown=markdown, parser="python-docx")


def _html_table(table: Tag) -> str:
    if table.find("table"):
        raise validation("Nested HTML tables cannot be extracted reliably.")
    source_rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    rows: list[list[str]] = []
    # Remaining row spans occupy their original columns in following rows.
    spans: dict[int, tuple[int, str]] = {}
    has_header = False
    output_chars = 0
    for source_row in source_rows:
        cells = source_row.find_all(["th", "td"], recursive=False)
        if not cells:
            if spans:
                raise validation("The HTML table has an unsupported empty row within a cell span.")
            continue
        if not rows:
            has_header = all(cell.name == "th" for cell in cells)
        row: dict[int, str] = {}
        next_spans: dict[int, tuple[int, str]] = {}
        for col, (remaining, text) in spans.items():
            row[col] = text
            if remaining > 1:
                next_spans[col] = (remaining - 1, text)
        col = 0
        for cell in cells:
            while col in row:
                col += 1
            try:
                rowspan = int(cell.get("rowspan", 1))
                colspan = int(cell.get("colspan", 1))
            except (TypeError, ValueError) as exc:
                raise validation("The HTML table has an unsupported cell span.") from exc
            if not (1 <= rowspan <= 100 and 1 <= colspan <= 100):
                raise validation("The HTML table has an unsupported cell span.")
            text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).replace("|", "\\|")
            for index in range(col, col + colspan):
                if index in row or index >= 100:
                    raise validation("The HTML table has overlapping or oversized cell spans.")
                row[index] = text
                if rowspan > 1:
                    next_spans[index] = (rowspan - 1, text)
            col += colspan
        spans = next_spans
        # Span expansion can amplify a small HTML response into a huge table.
        output_chars += sum(len(value) for value in row.values())
        if output_chars > 15 * 1024 * 1024:
            raise validation("The expanded HTML table is too large to ingest.")
        rows.append([row.get(index, "") for index in range(max(row, default=-1) + 1)])
    if spans:
        raise validation("The HTML table has a cell span beyond its final row.")
    if not rows or not any(cell for row in rows for cell in row):
        return ""
    return _markdown_table(rows, has_header=has_header)


def _parse_html(data: bytes, title: str) -> ParsedDocument:
    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
        tag.decompose()
    page_title = soup.title.get_text(strip=True) if soup.title else title
    if soup.head:
        soup.head.decompose()

    block_names = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section", "article",
                   "main", "blockquote", "ul", "ol", "li", "table", "pre", "hr"}

    def content(node: Tag) -> list[str]:
        blocks: list[str] = []
        inline: list[str | None] = []

        def flush() -> None:
            lines: list[str] = []
            fragments: list[str] = []
            for fragment in inline:
                if fragment is None:
                    lines.append(re.sub(r"\s+", " ", "".join(fragments)).strip())
                    fragments.clear()
                else:
                    fragments.append(fragment)
            lines.append(re.sub(r"\s+", " ", "".join(fragments)).strip())
            text = "\n".join(lines).strip()
            if text:
                blocks.append(text)
            inline.clear()

        def walk(child) -> None:
            if isinstance(child, Comment):
                return
            if isinstance(child, NavigableString):
                inline.append(str(child))
            elif isinstance(child, Tag):
                if child.name == "br":
                    inline.append(None)
                elif child.name in block_names:
                    flush()
                    blocks.extend(render(child))
                else:
                    for descendant in child.children:
                        walk(descendant)

        for child in node.children:
            walk(child)
        flush()
        return blocks

    def render(node: Tag) -> list[str]:
        name = node.name
        if name == "table":
            table = _html_table(node)
            caption = node.find("caption", recursive=False)
            label = caption.get_text(" ", strip=True) if caption else ""
            return ([label] if label else []) + ([table] if table else [])
        if name == "pre":
            text = node.get_text()
            if not text.strip():
                return []
            fence = "`" * max(3, max((len(m.group()) for m in re.finditer(r"`+", text)), default=0) + 1)
            return [f"{fence}\n{text}\n{fence}"]
        if name in {"ul", "ol"}:
            lines: list[str] = []
            items = node.find_all("li", recursive=False)
            step = -1 if node.has_attr("reversed") else 1
            try:
                index = int(node.get("start", len(items) if step < 0 else 1))
            except (TypeError, ValueError):
                index = len(items) if step < 0 else 1
            for item in items:
                if name == "ol" and item.has_attr("value"):
                    try:
                        index = int(item["value"])
                    except (TypeError, ValueError):
                        pass
                parts = content(item)
                marker = f"{index}. " if name == "ol" else "- "
                index += step
                if not parts:
                    continue
                first, *rest = parts
                lines.append(marker + first.replace("\n", "\n" + " " * len(marker)))
                for part in rest:
                    lines.append("\n".join(" " * len(marker) + line for line in part.splitlines()))
            return ["\n".join(lines)] if lines else []
        if name and re.fullmatch(r"h[1-6]", name):
            text = " ".join(content(node)).strip()
            return [f"{'#' * min(int(name[1]), 4)} {text}"] if text else []
        if name == "blockquote":
            blocks = content(node)
            return ["\n".join("> " + line if line else ">" for line in "\n\n".join(blocks).splitlines())] if blocks else []
        if name == "hr":
            return []
        return content(node)

    blocks = content(soup.body or soup)
    markdown = "\n\n".join(blocks)
    if not markdown.strip():
        raise validation("The page contains no readable text.")
    return ParsedDocument(title=page_title or title, markdown=markdown, parser="beautifulsoup")


def _parse_slides(data: bytes, title: str) -> ParsedDocument:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    blocks: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        blocks.append(f"## Slide {index}")
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                blocks.append(shape.text.strip())
    markdown = "\n\n".join(blocks)
    if not markdown.strip():
        raise validation("The presentation contains no extractable text.")
    return ParsedDocument(title=title, markdown=markdown, parser="python-pptx")


def parse_bytes(
    filename: str, mime_type: str | None, data: bytes, fallback_title: str, *, kind: str | None = None,
) -> ParsedDocument:
    suffix = Path(filename).suffix.lower()
    title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or fallback_title
    mime = (mime_type or "").split(";", 1)[0].strip().lower()

    # URL fetches may not have a meaningful extension, so retain MIME fallback.
    # Uploaded sources pass their validated kind, avoiding mismatched headers.
    if kind is None:
        if suffix == ".pdf" or mime == "application/pdf":
            kind = "pdf"
        elif suffix == ".docx" or "wordprocessingml" in mime:
            kind = "docx"
        elif suffix in {".html", ".htm"} or mime == "text/html":
            kind = "html"
        elif suffix == ".pptx" or "presentationml" in mime:
            kind = "pptx"
        elif suffix in UPLOAD_FORMATS or mime.startswith("text/") or mime in {
            "application/json", "application/x-yaml", "application/yaml",
        }:
            kind = "text"
    if kind == "pdf":
        return _parse_pdf(data, title)
    if kind == "docx":
        return _parse_docx(data, title)
    if kind == "html":
        return _parse_html(data, title)
    if kind == "pptx":
        return _parse_slides(data, title)
    if kind in {"text", "markdown"}:
        text = _decode(data)
        if suffix in {".csv", ".log"}:
            text = "```\n" + text + "\n```"
        return ParsedDocument(title=title, markdown=text, parser="text")
    raise validation(
        f"Unsupported file type '{suffix or mime or 'unknown'}'. "
        "Upload PDF, DOCX, PPTX, HTML, Markdown, text, or a URL."
    )


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise validation("Only http and https URLs can be ingested.")
    if not parsed.hostname:
        raise validation("That URL has no hostname.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise validation(f"Could not resolve {parsed.hostname}.") from exc
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise validation("Private and internal addresses cannot be fetched.")


async def fetch_url(url: str, max_bytes: int, timeout: float) -> tuple[bytes, str, str]:
    """Fetch a URL with SSRF protection, redirect checks, and a size cap."""
    current = url
    headers = {"user-agent": "HarbourKnowledgeBot/0.1 (+support knowledge ingestion)"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(4):
            _assert_public_url(current)
            response = await client.get(current, headers=headers)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise validation("The URL redirected without a destination.")
                current = str(response.url.join(location))
                continue
            if response.status_code >= 400:
                raise validation(f"The URL returned HTTP {response.status_code}.")
            data = response.content
            if len(data) > max_bytes:
                raise validation("That page is larger than the 15 MB ingestion limit.")
            final = str(response.url)
            return data, response.headers.get("content-type", "text/html"), final
    raise validation("The URL redirected too many times.")
