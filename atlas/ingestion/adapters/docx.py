"""DOCX -> NormalizedDocument with heading-aware sections.

Body paragraphs and tables are read in document order. Headings (Title,
Heading 1..9) set the `heading_path` carried by every following section;
tables become their own `table` sections (cells joined by " | ").
"""

from __future__ import annotations

import re
from pathlib import Path

from atlas.ingestion.adapters.base import UnsupportedSource
from atlas.ingestion.normalized import NormalizedDocument, document_id_for
from atlas.ingestion.segmentation import assemble, headed_blocks

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _heading_level(style_name: str | None) -> int | None:
    if not style_name:
        return None
    if style_name == "Title":
        return 0
    m = re.fullmatch(r"Heading (\d)", style_name)
    return int(m.group(1)) if m else None


class DocxAdapter:
    name = "docx"

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return path.suffix.lower() == ".docx"

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            document = docx.Document(str(path))
        except Exception as exc:
            raise UnsupportedSource(f"{path}: unreadable DOCX ({exc})") from exc

        items: list[tuple] = []
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                para = Paragraph(child, document)
                items.append((_heading_level(para.style.name if para.style is not None else None), para.text))
            elif tag == "tbl":
                rows = []
                for row in Table(child, document).rows:
                    cells = [c.text.strip() for c in row.cells]
                    rows.append(" | ".join(dict.fromkeys(cells)))  # merged cells repeat; keep once
                items.append((None, "\n".join(r for r in rows if r.strip()), "table"))
        raw_text, sections = assemble(headed_blocks(items))

        props = document.core_properties
        title = props.title or next((s.text for s in sections if s.kind == "heading"), None) or path.stem
        external_id = path.as_posix()
        return NormalizedDocument(
            document_id=document_id_for("file", external_id),
            source_system="file",
            source_type="docx",
            source_external_id=external_id,
            title=title,
            uri=external_id,
            raw_text=raw_text,
            created_at=props.created,
            updated_at=props.modified,
            metadata={"filename": path.name, "mime_type": DOCX_MIME,
                      "file_metadata_author": props.author or None,
                      "file_metadata_last_modified_by": props.last_modified_by or None},
            sections=sections,
        )
