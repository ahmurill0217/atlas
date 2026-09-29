"""What every extractor (code, rules, model) must produce: candidates.

Candidates are proposals. They carry their provenance class, the extractor
that produced them and its version, and a pointer to evidence — but no
canonical ids and no authority. Only the compiler turns them into graph facts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    section_ordinal: int | None = None
    # STRUCTURED_SOURCE: the field the fact came from, e.g. "calendar_invitees[2].email".
    source_field: str | None = None
    # Text provenance: the exact supporting quote; offsets are computed by the
    # compiler, never trusted from a model.
    evidence_text: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    page_number: int | None = None
    observed_at: datetime | None = None   # source time of the fact (sent / meeting time)
    reviewer: str | None = None           # HUMAN_CONFIRMED only


class CandidateEntity(BaseModel):
    local_id: str                       # unique within one document's extraction
    suggested_type: str                 # mapped through the ontology, never trusted as-is
    name: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)  # identity_field -> value
    properties: dict = Field(default_factory=dict)
    roles: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    provenance_class: str
    source_field: str | None = None
    extractor: str
    extractor_version: str
    confidence: float = 1.0


class CandidateEdge(BaseModel):
    local_id: str
    source_local_id: str
    target_local_id: str
    suggested_relation: str
    provenance_class: str
    evidence: EvidenceRef
    extractor: str
    extractor_version: str
    confidence: float = 1.0
    properties: dict = Field(default_factory=dict)


class CandidateSet(BaseModel):
    """One extractor's output for one document version (serializable, inspectable)."""

    document_id: uuid.UUID
    document_version_id: uuid.UUID
    extractor: str
    extractor_version: str
    entities: list[CandidateEntity] = Field(default_factory=list)
    edges: list[CandidateEdge] = Field(default_factory=list)
