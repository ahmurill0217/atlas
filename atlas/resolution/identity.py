"""Deterministic entity resolution (Phase 1: identifiers first, names never merge).

  1. identifiers  -> exact lookup in entity_external_ids
       one entity         => MATCHED
       several entities   => REVIEW (POSSIBLE_DUPLICATE: identifiers disagree)
       none               => CREATE (strong identity); a same-named weak entity
                             is flagged POSSIBLE_DUPLICATE, never merged
  2. name only    -> first the mention key (source system + document + mention), so
                             re-processing the same mention finds the entity it created;
                             then normalized name / alias lookup within the type
       none               => CREATE (weak, 'name_only' identity; the mention key is stored)
       one or more        => REVIEW (AMBIGUOUS_ENTITY_MATCH) — names alone never merge
Scored matching (context, fuzzy, embeddings, LLM adjudication) arrives in Phase 4
and plugs in before the REVIEW outcomes; this policy stays the safe default.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from atlas.db.models import Entity, EntityAlias, EntityExternalId
from atlas.ontology.models import Ontology
from atlas.resolution.normalize import normalize_identifier, normalize_name


class Outcome(str, Enum):
    MATCHED = "MATCHED"
    CREATE = "CREATE"
    REVIEW = "REVIEW"


class Resolution(BaseModel):
    outcome: Outcome
    entity_id: uuid.UUID | None = None
    review_type: str | None = None
    reason: str
    candidates: list[uuid.UUID] = Field(default_factory=list)   # possible matches (for review)
    possible_same_as: list[uuid.UUID] = Field(default_factory=list)  # created, but maybe a duplicate
    identifiers: dict[str, str] = Field(default_factory=dict)       # normalized


# Internal, source-native identifier for name-only entities: never an ontology
# identity field and never upgrades identity strength.
MENTION_KEY = "source_mention"


class IdentityResolver:
    def __init__(self, session: Session, ontology: Ontology):
        self.session, self.ontology = session, ontology

    def resolve(self, entity_type: str, name: str | None, identifiers: dict[str, str],
                mention_key: str | None = None) -> Resolution:
        ids = {t: normalize_identifier(t, v) for t, v in sorted(identifiers.items()) if v}
        family = self.ontology.ancestors(entity_type)
        if ids:
            owners = sorted({row.entity_id for row in self.session.execute(
                select(EntityExternalId.entity_id).join(Entity, Entity.id == EntityExternalId.entity_id)
                .where(or_(*[(EntityExternalId.identifier_type == t) & (EntityExternalId.value == v)
                             for t, v in ids.items()]), Entity.status == "active"))})
            if len(owners) == 1:
                return Resolution(outcome=Outcome.MATCHED, entity_id=owners[0], identifiers=ids,
                                  reason=f"identifier match {ids}")
            if len(owners) > 1:
                return Resolution(outcome=Outcome.REVIEW, review_type="POSSIBLE_DUPLICATE", candidates=owners,
                                  identifiers=ids, reason=f"identifiers {ids} belong to {len(owners)} different entities")
            weak = self._by_name(family, name, weak_only=True)
            return Resolution(outcome=Outcome.CREATE, identifiers=ids, possible_same_as=weak,
                              reason="new identifier" + (" (same name as an unconfirmed entity)" if weak else ""))
        if mention_key:
            same = self.session.execute(select(EntityExternalId.entity_id).where(
                EntityExternalId.identifier_type == MENTION_KEY, EntityExternalId.value == mention_key)).scalar_one_or_none()
            if same:
                return Resolution(outcome=Outcome.MATCHED, entity_id=same, reason="same source mention re-processed")
        matches = self._by_name(family, name)
        if not matches:
            return Resolution(outcome=Outcome.CREATE, reason="no identifiers; no entity with this name")
        return Resolution(outcome=Outcome.REVIEW, review_type="AMBIGUOUS_ENTITY_MATCH", candidates=matches,
                          reason=f"no identifiers; name matches {len(matches)} existing entit{'y' if len(matches) == 1 else 'ies'}")

    def _by_name(self, family: list[str], name: str | None, weak_only: bool = False) -> list[uuid.UUID]:
        if not name:
            return []
        key = normalize_name(name)
        query = (select(Entity.id).outerjoin(EntityAlias, EntityAlias.entity_id == Entity.id)
                 .where(Entity.entity_type.in_(family), Entity.status == "active",
                        or_(Entity.normalized_name == key, EntityAlias.normalized_alias == key)))
        if weak_only:
            query = query.where(Entity.identity_strength == "name_only")
        return sorted(set(self.session.execute(query).scalars()))
