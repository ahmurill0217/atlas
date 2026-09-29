"""Deterministic entity resolution: extracted entity -> canonical entity.

The LLM proposes names; this module decides identity. Steps run in order and
the first step that produces a hit decides:

  1. exact normalized canonical name
  2. alias match: extracted name vs. existing aliases, then extracted
     aliases vs. existing names + aliases
  3. fuzzy string similarity (optional)
  4. embedding similarity (optional)
  5. adjudication hook for leftover ambiguity (optional, e.g. an LLM)

Within a step, more than one distinct candidate is AMBIGUOUS unless the entity
type breaks the tie. AMBIGUOUS never merges: the caller creates a separate,
flagged entity and the candidates are logged for review.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import re

from rapidfuzz import fuzz

from brain.config import Settings
from brain.embeddings.embedder import Embedder
from brain.extraction.models import ExtractedEntity
from brain.graph.repository import EntityRow, GraphRepository
from brain.normalize import compact_name, normalize_name, numeric_tokens


class Decision(str, Enum):
    MATCH_EXISTING = "MATCH_EXISTING"
    CREATE_NEW = "CREATE_NEW"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class Candidate:
    entity_id: uuid.UUID
    canonical_name: str
    entity_type: str
    method: str
    score: float
    matched_form: str | None = None

    def as_dict(self) -> dict:
        return {"entity_id": str(self.entity_id), "canonical_name": self.canonical_name,
                "entity_type": self.entity_type, "method": self.method,
                "score": round(self.score, 4), "matched_form": self.matched_form}


@dataclass
class ResolutionDecision:
    decision: Decision
    entity_id: uuid.UUID | None
    method: str
    score: float | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)


# (extracted entity, candidates) -> chosen entity id, or None to stay ambiguous.
Adjudicator = Callable[[ExtractedEntity, list[Candidate]], uuid.UUID | None]


_SIZE_ONLY = re.compile(r"^\d+(\.\d+)?\s?[kmbt]?$")  # "137b", "540 b", "2", "422m"
_CITATION = re.compile(r"\bet al\b|\b(19|20)\d{2}\b")


def _singular_tokens(normalized: str) -> set[str]:
    return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in normalized.split()}


def acceptable_alias(alias: str, name: str) -> bool:
    """Reject aliases that would cause false merges later:
    - a proper token subset/superset of the name ('KRAS' for 'KRAS G12C',
      'Universal Transformers' for 'Transformer'; plurals ignored)
    - a different numeric designation ('CodeBreaK 200' for 'CodeBreaK 100')
    - a bare size ('137B' for 'LaMDA'), which is not a name at all
    - a citation ('Rajpurkar et al., 2016' for 'SQuAD')"""
    a, n = normalize_name(alias), normalize_name(name)
    if len(a) < 2 or a == n or _SIZE_ONLY.match(a) or _CITATION.search(a):
        return False
    a_tokens, n_tokens = _singular_tokens(a), _singular_tokens(n)
    if a_tokens == n_tokens or a_tokens < n_tokens or n_tokens < a_tokens:
        return a_tokens == n_tokens  # plural/singular variant of the same name is fine
    if numeric_tokens(alias) and numeric_tokens(name) and numeric_tokens(alias) != numeric_tokens(name):
        return False
    return True


class EntityResolver:
    def __init__(
        self,
        repo: GraphRepository,
        settings: Settings,
        embedder: Embedder | None = None,
        adjudicator: Adjudicator | None = None,
    ):
        self.repo = repo
        self.settings = settings
        self.embedder = embedder
        self.adjudicator = adjudicator

    def resolve(self, entity: ExtractedEntity, entity_type: str) -> ResolutionDecision:
        """`entity_type` is the already-normalized type of the extracted entity."""
        for step in (self._exact, self._alias, self._fuzzy, self._embedding):
            decision = step(entity, entity_type)
            if decision is not None:
                if decision.decision is Decision.AMBIGUOUS and self.adjudicator:
                    return self._adjudicate(entity, decision)
                return decision
        return ResolutionDecision(Decision.CREATE_NEW, None, "none", None, "no candidate matched")

    # --- steps ---------------------------------------------------------------------

    def _exact(self, entity: ExtractedEntity, entity_type: str) -> ResolutionDecision | None:
        rows = self.repo.entities_by_normalized_name(normalize_name(entity.name))
        cands = [Candidate(r.id, r.canonical_name, r.entity_type, "exact_name", 1.0) for r in rows]
        return self._decide(cands, entity_type, "exact_name")

    def _alias(self, entity: ExtractedEntity, entity_type: str) -> ResolutionDecision | None:
        # The extracted name is the LLM's primary claim; its aliases are
        # secondary and occasionally wrong ("Like Lumakras, Krazati binds...").
        # So a unique hit on the name wins before aliases are consulted.
        name = normalize_name(entity.name)
        hits = self.repo.entities_by_surface_forms([name])
        cands = [Candidate(r.id, r.canonical_name, r.entity_type, "alias", 1.0, form) for r, form in hits]
        decision = self._decide(cands, entity_type, "alias")
        if decision is not None:
            return decision
        forms = [normalize_name(a) for a in entity.aliases if acceptable_alias(a, entity.name)]
        hits = self.repo.entities_by_surface_forms(list(dict.fromkeys(forms)))
        cands = [Candidate(r.id, r.canonical_name, r.entity_type, "extracted_alias", 1.0, form) for r, form in hits]
        return self._decide(cands, entity_type, "extracted_alias")

    def _fuzzy(self, entity: ExtractedEntity, entity_type: str) -> ResolutionDecision | None:
        if not self.settings.resolution_fuzzy_enabled:
            return None
        name = normalize_name(entity.name)
        cands: list[Candidate] = []
        # Spacing-only differences ("AMG510" vs "AMG 510") are treated as a hit
        # regardless of type; real fuzzy matches must also agree on type.
        for row, form in self.repo.compact_matches(compact_name(entity.name)):
            cands.append(Candidate(row.id, row.canonical_name, row.entity_type, "compact", 100.0, form))
        for row, form in self.repo.fuzzy_candidates(name):
            if row.entity_type != entity_type or numeric_tokens(form) != numeric_tokens(name):
                continue
            score = fuzz.ratio(name, form)
            if score >= self.settings.resolution_fuzzy_threshold:
                cands.append(Candidate(row.id, row.canonical_name, row.entity_type, "fuzzy", score, form))
        return self._decide(cands, entity_type, "fuzzy")

    def _embedding(self, entity: ExtractedEntity, entity_type: str) -> ResolutionDecision | None:
        if not (self.settings.resolution_embedding_enabled and self.embedder):
            return None
        [vector] = self.embedder.embed([entity_embedding_text(entity.name, entity_type, entity.description)])
        s = self.settings
        cands = [
            Candidate(r.id, r.canonical_name, r.entity_type, "embedding", sim)
            for r, sim in self.repo.nearest_by_embedding(vector)
            if sim >= s.resolution_embedding_ambiguous and numeric_tokens(r.canonical_name) == numeric_tokens(entity.name)
        ]
        if not cands:
            return None
        strong = [c for c in cands if c.score >= s.resolution_embedding_match]
        if len(strong) == 1 and strong[0].entity_type == entity_type:
            c = strong[0]
            return ResolutionDecision(Decision.MATCH_EXISTING, c.entity_id, "embedding", c.score,
                                      f"embedding similarity {c.score:.3f} >= {s.resolution_embedding_match}", cands)
        return ResolutionDecision(Decision.AMBIGUOUS, None, "embedding", max(c.score for c in cands),
                                  "embedding candidates in the ambiguous band", cands)

    # --- helpers --------------------------------------------------------------------

    @staticmethod
    def _decide(cands: list[Candidate], entity_type: str, method: str) -> ResolutionDecision | None:
        by_id: dict[uuid.UUID, Candidate] = {}
        for c in cands:
            if c.entity_id not in by_id or c.score > by_id[c.entity_id].score:
                by_id[c.entity_id] = c
        unique = list(by_id.values())
        if not unique:
            return None
        if len(unique) == 1:
            c = unique[0]
            return ResolutionDecision(Decision.MATCH_EXISTING, c.entity_id, method, c.score,
                                      f"single {method} candidate '{c.canonical_name}'", unique)
        same_type = [c for c in unique if c.entity_type == entity_type]
        if len(same_type) == 1:
            c = same_type[0]
            return ResolutionDecision(Decision.MATCH_EXISTING, c.entity_id, f"{method}+type", c.score,
                                      f"{len(unique)} {method} candidates; type {entity_type} breaks the tie", unique)
        return ResolutionDecision(Decision.AMBIGUOUS, None, method, max(c.score for c in unique),
                                  f"{len(unique)} distinct {method} candidates", unique)

    def _adjudicate(self, entity: ExtractedEntity, decision: ResolutionDecision) -> ResolutionDecision:
        chosen = self.adjudicator(entity, decision.candidates)
        if chosen is None or chosen not in {c.entity_id for c in decision.candidates}:
            return decision
        return ResolutionDecision(Decision.MATCH_EXISTING, chosen, f"{decision.method}+adjudicated",
                                  decision.score, "adjudicator chose among ambiguous candidates",
                                  decision.candidates)


def entity_embedding_text(name: str, entity_type: str, description: str | None) -> str:
    return f"{name} ({entity_type}): {description or ''}".strip()


__all__ = [
    "Adjudicator",
    "Candidate",
    "Decision",
    "EntityResolver",
    "EntityRow",
    "ResolutionDecision",
    "acceptable_alias",
    "entity_embedding_text",
]
