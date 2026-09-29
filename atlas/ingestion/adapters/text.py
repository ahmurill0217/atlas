"""Plain text / Markdown -> NormalizedDocument.

Markdown headings (#, ##, ...) become heading sections and give every
following paragraph its `heading_path`; paragraphs split on blank lines.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from atlas.ingestion.normalized import NormalizedDocument, document_id_for
from atlas.ingestion.segmentation import assemble, headed_blocks

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


class TextAdapter:
    name = "text"
    extensions = {".txt", ".md", ".markdown"}

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return path.suffix.lower() in self.extensions

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        text = unicodedata.normalize("NFC", path.read_text(encoding="utf-8")).replace("\r\n", "\n").replace("\x00", "")
        items: list[tuple] = []
        for para in re.split(r"\n\s*\n", text):
            for line_group in _split_headings(para):
                items.append(line_group)
        raw_text, sections = assemble(headed_blocks(items))
        title = next((s.text for s in sections if s.kind == "heading"), None) or path.stem
        external_id = path.as_posix()
        return NormalizedDocument(
            document_id=document_id_for("file", external_id),
            source_system="file",
            source_type="text",
            source_external_id=external_id,
            title=title,
            uri=external_id,
            raw_text=raw_text,
            metadata={"filename": path.name, "mime_type": "text/markdown" if path.suffix.lower() != ".txt" else "text/plain"},
            sections=sections,
        )


def _split_headings(paragraph: str) -> list[tuple]:
    """A paragraph may start with (or be) Markdown heading lines."""
    out, body = [], []
    for line in paragraph.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            if body:
                out.append((None, " ".join(body)))
                body = []
            out.append((len(m.group(1)), m.group(2)))
        elif line.strip():
            body.append(line.strip())
    if body:
        out.append((None, " ".join(body)))
    return out
