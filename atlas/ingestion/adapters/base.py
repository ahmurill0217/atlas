"""Source adapters: source -> NormalizedDocument. No ontology knowledge here."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from atlas.ingestion.normalized import NormalizedDocument, Section


class SourceAdapter(Protocol):
    name: str

    def can_handle(self, path: Path, payload: dict | None) -> bool: ...

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument: ...


class UnsupportedSource(ValueError):
    pass


def build_text(parts: list[tuple[str, str, dict]]) -> tuple[str, list[Section]]:
    """Join (kind, text, metadata) parts into raw_text with exact section offsets."""
    raw, sections, cursor = [], [], 0
    for ordinal, (kind, text, metadata) in enumerate(p for p in parts if p[1].strip()):
        text = text.strip()
        if raw:
            raw.append("\n\n")
            cursor += 2
        sections.append(Section(ordinal=ordinal, kind=kind, text=text, start_char=cursor,
                                end_char=cursor + len(text), metadata=metadata))
        raw.append(text)
        cursor += len(text)
    return "".join(raw), sections
