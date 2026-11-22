from __future__ import annotations

import io
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..errors import validation


@dataclass
class ParsedDocument:
    title: str
    markdown: str
    parser: str
    page_count: int = 0
    metadata: dict = field(default_factory=dict)


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".log"}


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
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"<!-- page: {number} -->\n\n{text.strip()}")
    markdown = "\n\n".join(pages)
    if len(markdown.replace("<!--", "").strip()) < 40:
        raise validation("The PDF has no extractable text layer. It needs OCR before it can be ingested.")
    markdown = _infer_headings(markdown)
    return ParsedDocument(
        title=title,
        markdown=markdown,
        parser="pypdf",
        page_count=len(reader.pages),
        metadata={"page_count": len(reader.pages)},
    )


def _parse_docx(data: bytes, title: str) -> ParsedDocument:
    from docx import Document

    document = Document(io.BytesIO(data))
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "").lower()
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "2"
            blocks.append(f"{'#' * min(int(level), 4)} {text}")
        elif style.startswith("list"):
            blocks.append(f"- {text}")
        else:
            blocks.append(text)
    for table in document.tables:
        rows: list[str] = []
        for row in table.rows:
            cells = [cell.text.strip().replace("|", "\\|") for cell in row.cells]
            rows.append("| " + " | ".join(cells) + " |")
        if rows:
            header = "| " + " | ".join("" for _ in table.rows[0].cells) + " |"
            blocks.append("\n".join([rows[0], header, *rows[1:]]))
    markdown = "\n\n".join(blocks)
    if not markdown.strip():
        raise validation("The document contains no extractable text.")
    return ParsedDocument(title=title, markdown=markdown, parser="python-docx")


def _parse_html(data: bytes, title: str) -> ParsedDocument:
    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
        tag.decompose()
    page_title = soup.title.get_text(strip=True) if soup.title else title

    blocks: list[str] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "table", "pre"]):
        if element.name == "li":
            blocks.append(f"- {element.get_text(' ', strip=True)}")
        elif element.name and element.name.startswith("h"):
            level = int(element.name[1])
            blocks.append(f"{'#' * min(level, 4)} {element.get_text(' ', strip=True)}")
        elif element.name == "table":
            rows = []
            for tr in element.find_all("tr"):
                cells = [td.get_text(" ", strip=True).replace("|", "\\|") for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                blocks.append("\n".join(rows))
        elif element.name == "pre":
            blocks.append(f"```\n{element.get_text()}\n```")
        else:
            text = element.get_text(" ", strip=True)
            if text:
                blocks.append(text)
    markdown = "\n\n".join(blocks) or soup.get_text("\n", strip=True)
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


def parse_bytes(filename: str, mime_type: str | None, data: bytes, fallback_title: str) -> ParsedDocument:
    suffix = Path(filename).suffix.lower()
    title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or fallback_title
    mime = (mime_type or "").lower()

    if suffix == ".pdf" or mime == "application/pdf":
        return _parse_pdf(data, title)
    if suffix in {".docx"} or "wordprocessingml" in mime:
        return _parse_docx(data, title)
    if suffix in {".html", ".htm"} or "text/html" in mime:
        return _parse_html(data, title)
    if suffix in {".pptx"} or "presentationml" in mime:
        return _parse_slides(data, title)
    if suffix in TEXT_EXTENSIONS or mime.startswith("text/") or mime in {
        "application/json", "application/x-yaml", "application/yaml",
    }:
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
