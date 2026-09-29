"""Adapter registry. Add a connector = add an adapter here; graph semantics are untouched."""

from __future__ import annotations

import json
from pathlib import Path

from atlas.ingestion.adapters.base import SourceAdapter, UnsupportedSource
from atlas.ingestion.adapters.docx import DocxAdapter
from atlas.ingestion.adapters.email_json import EmailJsonAdapter
from atlas.ingestion.adapters.generic_json import GenericJsonAdapter
from atlas.ingestion.adapters.meeting_json import MeetingJsonAdapter
from atlas.ingestion.adapters.pdf import PdfAdapter
from atlas.ingestion.adapters.text import TextAdapter
from atlas.ingestion.normalized import NormalizedDocument

ADAPTERS: list[SourceAdapter] = [EmailJsonAdapter(), MeetingJsonAdapter(), GenericJsonAdapter(),
                                  PdfAdapter(), DocxAdapter(), TextAdapter()]
SUPPORTED = {".json", ".txt", ".md", ".markdown", ".pdf", ".docx"}


def discover(path: str | Path) -> list[Path]:
    root = Path(path)
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)


def normalize_file(path: Path) -> NormalizedDocument:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else None
    for adapter in ADAPTERS:
        if adapter.can_handle(path, payload):
            return adapter.normalize(path, payload)
    raise UnsupportedSource(f"{path}: no adapter recognises this source")


__all__ = ["ADAPTERS", "UnsupportedSource", "discover", "normalize_file"]
