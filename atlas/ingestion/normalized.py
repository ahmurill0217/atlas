"""The one intermediate representation every source adapter produces.

Everything downstream (segmentation, extraction, graph compilation) reads only
this model, so adding a connector never changes graph semantics.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

DOCUMENT_NAMESPACE = uuid.UUID("0b9d3c1e-5a52-4d7e-9a40-6c1f0a8e2b77")


def document_id_for(source_system: str, source_external_id: str) -> uuid.UUID:
    """Stable id from the source's own identity (same message => same document)."""
    return uuid.uuid5(DOCUMENT_NAMESPACE, f"{source_system}:{source_external_id}")


class Participant(BaseModel):
    name: str | None = None
    email: str | None = None  # lowercased by adapters
    # sender | to | cc | bcc | invitee | recorder | speaker | author | owner
    role: str
    is_external: bool | None = None
    team: str | None = None
    source_field: str         # where in the source this participant came from


class Section(BaseModel):
    ordinal: int
    kind: str                 # body | quoted | summary | transcript_turn | action_item | paragraph | page
    text: str
    start_char: int | None = None   # offsets into NormalizedDocument.raw_text
    end_char: int | None = None
    metadata: dict = Field(default_factory=dict)


class NormalizedDocument(BaseModel):
    document_id: uuid.UUID
    source_system: str        # gmail | fathom | file | ...
    source_type: str          # email | meeting | text | pdf | ...
    source_external_id: str
    title: str | None = None
    uri: str | None = None
    raw_text: str = ""
    author: Participant | None = None
    participants: list[Participant] = Field(default_factory=list)
    created_at: datetime | None = None   # source time (sent / meeting start / file created)
    updated_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)
    sections: list[Section] = Field(default_factory=list)
    permissions: dict = Field(default_factory=dict)  # passed through from the platform, never interpreted

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        """Source times without a zone are taken as UTC so comparisons are always valid."""
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    def checksum(self) -> str:
        """Content checksum: same source content => same checksum => no-op re-ingest."""
        payload = self.model_dump(mode="json", exclude={"permissions"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
