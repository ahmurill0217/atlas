"""Plain text / Markdown file -> NormalizedDocument (paragraph sections)."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from atlas.ingestion.adapters.base import build_text
from atlas.ingestion.normalized import NormalizedDocument, document_id_for


class TextAdapter:
    name = "text"
    extensions = {".txt", ".md", ".markdown"}

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return path.suffix.lower() in self.extensions

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        text = unicodedata.normalize("NFC", path.read_text(encoding="utf-8")).replace("\r\n", "\n").replace("\x00", "")
        paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        raw_text, sections = build_text([("paragraph", p, {}) for p in paragraphs])
        heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        external_id = path.as_posix()
        return NormalizedDocument(
            document_id=document_id_for("file", external_id),
            source_system="file",
            source_type="text",
            source_external_id=external_id,
            title=heading.group(1).strip() if heading else path.stem,
            uri=external_id,
            raw_text=raw_text,
            metadata={"filename": path.name},
            sections=sections,
        )
