"""Relationship validation: an extracted edge must earn its way into the graph.

Checks, in order (the first failure rejects the edge, with a reason):
  1. both endpoints resolved to canonical entities
  2. relationship type normalized (synonyms / inverses applied) and allowed
  3. no self-relationship (unless allowed)
  4. evidence present
  5. evidence grounded: it is (approximately) a quote from the chunk
  6. both endpoints are named in the chunk (or in the quote, if strict)
  7. an endpoint missing from the quote is not *substituted* by another
     entity of the same type that the quote does name ("AMG 510 inhibits
     KRAS G12C" cannot support an edge from adagrasib)

Accepted edges are then upserted by the pipeline: an existing
(source, type, target) edge gets the new evidence attached instead of a copy.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from brain_v0.config import Settings
from brain_v0.extraction.models import ExtractedRelationship
from brain_v0.normalize import normalize_name, normalize_relationship_type


@dataclass(frozen=True)
class ResolvedEndpoint:
    """What the validator needs to know about a resolved entity."""

    entity_id: uuid.UUID
    canonical_name: str
    surface_forms: tuple[str, ...]  # extracted name/aliases + known canonical name/aliases
    entity_type: str | None = None


@dataclass
class RelationshipDecision:
    accepted: bool
    reason: str
    source_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    relationship_type: str | None = None
    evidence: str | None = None
    details: dict = field(default_factory=dict)


def _norm_text(text: str) -> str:
    text = re.sub(r"[‘’“”\"'`]", "", text.casefold())
    text = re.sub(r"\.\.\.|…", " ", text)
    return " ".join(text.split())


def locate_mention(forms: tuple[str, ...], text: str) -> bool:
    """True if any surface form occurs in `text` as whole words (normalized),
    tolerating a plural suffix ('lung adenocarcinomas' mentions 'lung adenocarcinoma')."""
    haystack = f" {normalize_name(text)} "
    for form in forms:
        norm = normalize_name(form)
        if norm and re.search(rf" {re.escape(norm)}(?:s|es)? ", haystack):
            return True
    return False


class RelationshipValidator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.allowed = {normalize_relationship_type(t) for t in settings.allowed_relationship_types}

    def normalize_type(self, raw: str) -> tuple[str, bool]:
        """Return (canonical type, flipped). Inverse types flip the direction."""
        t = normalize_relationship_type(raw)
        synonyms = {normalize_relationship_type(k): normalize_relationship_type(v)
                    for k, v in self.settings.relationship_type_synonyms.items()}
        inverses = {normalize_relationship_type(k): normalize_relationship_type(v)
                    for k, v in self.settings.relationship_type_inverses.items()}
        if t in inverses:
            return synonyms.get(inverses[t], inverses[t]), True
        return synonyms.get(t, t), False

    def validate(
        self,
        rel: ExtractedRelationship,
        endpoints: dict[str, ResolvedEndpoint],
        chunk_text: str,
    ) -> RelationshipDecision:
        source, target = endpoints.get(rel.source_local_id), endpoints.get(rel.target_local_id)
        if source is None or target is None:
            return RelationshipDecision(False, "unresolved_endpoint")

        rel_type, flipped = self.normalize_type(rel.relationship_type)
        if flipped:
            source, target = target, source
        base = dict(source_id=source.entity_id, target_id=target.entity_id,
                    relationship_type=rel_type, evidence=rel.evidence)
        details: dict = {"raw_type": rel.relationship_type, "flipped": flipped}

        if self.allowed and rel_type not in self.allowed:
            return RelationshipDecision(False, "type_not_allowed", details=details, **base)
        if source.entity_id == target.entity_id and not self.settings.allow_self_relationships:
            return RelationshipDecision(False, "self_relationship", details=details, **base)
        if not rel.evidence or not rel.evidence.strip():
            return RelationshipDecision(False, "missing_evidence", details=details, **base)

        grounding = fuzz.partial_ratio(_norm_text(rel.evidence), _norm_text(chunk_text))
        details["grounding"] = round(grounding, 1)
        if grounding < self.settings.evidence_min_grounding:
            return RelationshipDecision(False, "evidence_not_in_source", details=details, **base)

        others = [e for e in dict.fromkeys(endpoints.values())
                  if e.entity_id not in (source.entity_id, target.entity_id)]
        for role, endpoint in (("source", source), ("target", target)):
            if locate_mention(endpoint.surface_forms, rel.evidence):
                details[f"{role}_mentioned"] = "in_quote"
                continue
            if not locate_mention(endpoint.surface_forms, chunk_text):
                details[f"{role}_mentioned"] = "missing"
                return RelationshipDecision(False, f"{role}_not_in_chunk", details=details, **base)
            details[f"{role}_mentioned"] = "in_chunk"
            if self.settings.evidence_require_endpoints_in_quote:
                return RelationshipDecision(False, f"{role}_not_in_evidence", details=details, **base)
            substitutes = [e.canonical_name for e in others
                           if e.entity_type and e.entity_type == endpoint.entity_type
                           and locate_mention(e.surface_forms, rel.evidence)]
            if substitutes:
                details[f"{role}_substituted_by"] = substitutes
                return RelationshipDecision(False, f"{role}_substituted_in_evidence", details=details, **base)

        return RelationshipDecision(True, "valid", details=details, **base)
