"""Generic structured JSON ("Atlas document") -> NormalizedDocument.

The format any connector can emit (e.g. Drive API metadata + exported text):

    {
      "atlas_document": "1.0",
      "source_system": "gdrive",            "id": "1AbC...",
      "source_type": "doc",                 "title": "...",  "uri": "...",
      "created_at": "...",                  "updated_at": "...",
      "author": {"name": "...", "email": "..."},
      "participants": [{"name": "...", "email": "...", "role": "owner|editor|commenter|viewer|..."}],
      "text": "..."                         -- or --
      "sections": [{"kind": "heading|paragraph|table|...", "text": "...", "level": 1, "metadata": {}}],
      "metadata": {...},                    "permissions": {...}
    }
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from atlas.ingestion.adapters.base import UnsupportedSource
from atlas.ingestion.normalized import NormalizedDocument, Participant, document_id_for
from atlas.ingestion.segmentation import Block, assemble, headed_blocks

SUPPORTED_VERSIONS = {"1.0"}


def _dt(value) -> datetime | None:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


def _participant(p: dict, role: str, field: str) -> Participant:
    email = (p.get("email") or "").strip().lower() or None
    return Participant(name=p.get("name"), email=email, role=p.get("role", role), source_field=field)


class GenericJsonAdapter:
    name = "generic_json"

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return bool(payload) and "atlas_document" in payload

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        if str(payload.get("atlas_document")) not in SUPPORTED_VERSIONS:
            raise UnsupportedSource(f"{path}: atlas_document version {payload.get('atlas_document')!r} not supported")
        missing = [k for k in ("source_system", "id") if not payload.get(k)]
        if missing or not (payload.get("text") or payload.get("sections")):
            raise UnsupportedSource(f"{path}: atlas_document needs source_system, id and text or sections (missing {missing})")

        author = _participant(payload["author"], "author", "author") if payload.get("author") else None
        participants = ([author] if author else []) + [
            _participant(p, p.get("role", "participant"), f"participants[{i}]")
            for i, p in enumerate(payload.get("participants") or [])]

        if payload.get("sections"):
            items = []
            for s in payload["sections"]:
                if s.get("kind") == "heading":
                    items.append((int(s.get("level", 1)), s.get("text", "")))
                else:
                    items.append((None, s.get("text", ""), s.get("kind", "paragraph")))
            blocks = headed_blocks(items)
        else:
            blocks = [Block("paragraph", p) for p in re.split(r"\n\s*\n", payload["text"]) if p.strip()]
        raw_text, sections = assemble(blocks)

        system, external_id = payload["source_system"], str(payload["id"])
        return NormalizedDocument(
            document_id=document_id_for(system, external_id),
            source_system=system,
            source_type=payload.get("source_type", "document"),
            source_external_id=external_id,
            title=payload.get("title"),
            uri=payload.get("uri"),
            raw_text=raw_text,
            author=author,
            participants=participants,
            created_at=_dt(payload.get("created_at")),
            updated_at=_dt(payload.get("updated_at")),
            metadata={**(payload.get("metadata") or {}), "mime_type": (payload.get("metadata") or {}).get("mime_type")},
            sections=sections,
            permissions=payload.get("permissions") or {},
        )
