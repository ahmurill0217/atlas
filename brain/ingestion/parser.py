"""Turn a file into normalized text plus a stable content hash."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ParsedDocument:
    source_uri: str
    title: str
    text: str
    content_hash: str
    metadata: dict = field(default_factory=dict)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_text(text: str, source_uri: str, title: str | None = None, **metadata) -> ParsedDocument:
    text = normalize_text(text)
    if title is None:
        heading = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
        title = heading.group(1).strip() if heading else Path(source_uri).stem
    return ParsedDocument(source_uri, title, text, content_hash(text), metadata)


_HEADER_LINE = re.compile(r"^(arXiv:|Published as|Under review|Journal of|Preprint|Proceedings|\d+$)", re.IGNORECASE)


def _first_line(text: str) -> str:
    """First line that looks like a title rather than a venue/arXiv header."""
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 5 and not _HEADER_LINE.match(line):
            return line[:200]
    return "untitled"


def _source_uri(path: Path) -> str:
    """Relative to the working directory when possible, so the same corpus
    maps to the same documents from any clone location."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


_REFERENCES_HEADING = re.compile(r"(?m)^\s*(?:\d+\.?\s*)?(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*$")


def clean_pdf_text(text: str) -> str:
    """Undo common PDF text-extraction artifacts."""
    text = unicodedata.normalize("NFKC", text).replace("\t", " ").replace("\u00a0", " ")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # hyphenated line breaks
    text = re.sub(r"\s?/([a-z])\.sc\b", lambda m: m.group(1).upper(), text)  # small caps: R/e.sc /t.sc -> RET
    text = re.sub(r"[ ]{2,}", " ", text)
    return text


def extract_pdf_text(path: Path) -> tuple[str, dict]:
    """Page texts joined by blank lines, cut at the References heading so
    bibliography entries do not flood extraction with citation-only entities."""
    import pypdf

    reader = pypdf.PdfReader(str(path))
    pages = [clean_pdf_text(page.extract_text() or "") for page in reader.pages]
    cut_page = None
    for i, page in enumerate(pages):
        if i >= 2 and _REFERENCES_HEADING.search(page):  # skip TOCs on the first pages
            pages[i] = page[: _REFERENCES_HEADING.search(page).start()]
            cut_page = i + 1
            pages = pages[: i + 1]
            break
    title = (reader.metadata.title if reader.metadata and reader.metadata.title else "") or ""
    meta = {"pages": len(reader.pages), "references_cut_at_page": cut_page}
    if len(title.strip()) >= 8:
        meta["pdf_title"] = title.strip()
    return "\n\n".join(p.strip() for p in pages if p.strip()), meta


def parse_file(path: str | Path) -> ParsedDocument:
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        text, meta = extract_pdf_text(path)
        return parse_text(text, source_uri=_source_uri(path), title=meta.get("pdf_title") or _first_line(text),
                          format="pdf", filename=path.name, **meta)
    return parse_text(
        path.read_text(encoding="utf-8"),
        source_uri=_source_uri(path),
        format=path.suffix.lower().lstrip("."),
        filename=path.name,
    )
