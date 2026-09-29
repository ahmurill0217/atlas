"""PDF -> NormalizedDocument with page-aware sections.

Every section records `page_number`, so text evidence can cite the page.
Headings and paragraph boundaries are recovered from the page text (blank lines
or layout cues); lines inside a paragraph are joined; oversized paragraphs are
split at sentence boundaries. A PDF with no
text layer (scanned) is still ingested, flagged `needs_ocr`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from atlas.ingestion.adapters.base import UnsupportedSource
from atlas.ingestion.normalized import NormalizedDocument, document_id_for
from atlas.ingestion.segmentation import Block, assemble

_LIST_ITEM = re.compile(r"^(\d{1,3}[.)]|[-•*▪])\s+\S")
_HEADER_LINE = re.compile(r"^(arXiv:|Published as|Under review|Preprint|Page \d+|\d+$)", re.IGNORECASE)


def clean_pdf_text(text: str) -> str:
    """Undo common extraction artifacts: ligatures (NFKC), tabs/nbsp, NUL bytes,
    hyphenated line breaks, small-caps runs ('R/e.sc /t.sc' -> 'RET')."""
    text = unicodedata.normalize("NFKC", text).replace("\x00", "").replace("\t", " ").replace(" ", " ")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"\s?/([a-z])\.sc\b", lambda m: m.group(1).upper(), text)
    return re.sub(r"[ ]{2,}", " ", text)


def page_blocks(text: str, page_number: int) -> list[Block]:
    """Paragraphs and headings of one page.

    Blank lines always separate paragraphs. Extractors such as pypdf often emit
    none, so layout cues are used too: a short line without closing punctuation
    is a heading; a numbered or bulleted line starts a new paragraph; a line that
    ends a sentence and is clearly shorter than a full line ends its paragraph.
    """
    blocks: list[Block] = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        full = max(len(line) for line in lines)
        current: list[str] = []

        def flush() -> None:
            if current:
                blocks.append(Block("paragraph", " ".join(current), {"page_number": page_number}))
                current.clear()

        for i, line in enumerate(lines):
            has_next = i + 1 < len(lines)
            heading_like = (len(line) <= 60 and not re.search(r"[.,;:!?\-]$", line) and has_next
                            and len(lines) > 1 and line[:1].isupper())
            if heading_like:
                flush()
                blocks.append(Block("heading", line, {"page_number": page_number}))
                continue
            if _LIST_ITEM.match(line):
                flush()  # each numbered / bulleted item is its own paragraph
            current.append(line)
            if re.search(r"[.!?:]$", line) and len(line) < 0.85 * full:
                flush()
        flush()
    return blocks


def _pdf_date(value) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _meaningful_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"^Microsoft (Word|PowerPoint) - ", "", title.strip())
    return title if len(title) >= 4 and title.lower() not in {"untitled", "document"} else None


class PdfAdapter:
    name = "pdf"

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return path.suffix.lower() == ".pdf"

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        import pypdf

        try:
            reader = pypdf.PdfReader(str(path))
            if reader.is_encrypted and not reader.decrypt(""):
                raise UnsupportedSource(f"{path}: encrypted PDF")
        except pypdf.errors.PdfReadError as exc:
            raise UnsupportedSource(f"{path}: unreadable PDF ({exc})") from exc

        blocks: list[Block] = []
        empty_pages: list[int] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = clean_pdf_text(page.extract_text() or "")
            except Exception:  # a single broken page must not lose the document
                text = ""
            page = page_blocks(text, number)
            if not page:
                empty_pages.append(number)
            blocks += page
        raw_text, sections = assemble(blocks)

        info = reader.metadata or {}
        first_line = next((s.text[:200] for s in sections if s.kind == "heading" and not _HEADER_LINE.match(s.text)),
                          None)
        external_id = path.as_posix()
        return NormalizedDocument(
            document_id=document_id_for("file", external_id),
            source_system="file",
            source_type="pdf",
            source_external_id=external_id,
            title=_meaningful_title(getattr(info, "title", None)) or first_line or path.stem,
            uri=external_id,
            raw_text=raw_text,
            created_at=_pdf_date(getattr(info, "creation_date", None)),
            updated_at=_pdf_date(getattr(info, "modification_date", None)),
            metadata={"filename": path.name, "mime_type": "application/pdf", "pages": len(reader.pages),
                      "pages_without_text": empty_pages, "needs_ocr": not sections,
                      # File-metadata authors are names only and often junk ("Microsoft Office
                      # User"); kept as metadata, never turned into graph edges.
                      "file_metadata_author": getattr(info, "author", None)},
            sections=sections,
        )
