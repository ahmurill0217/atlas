"""Deterministic entity resolution (Phase 1: identifiers first, names never merge).

  1. identifiers  -> exact lookup in entity_external_ids
       one entity         => MATCHED
       several entities   => REVIEW (POSSIBLE_DUPLICATE: identifiers disagree)
       none               => address patterns (Person, policy email_identity, 1.3):
                             a flast / lastname address and the ONE first.last person at
                             the same organization it fits are the same person => MATCHED
                             (method address_pattern); several fits => CREATE + POSSIBLE_DUPLICATE
                          => otherwise CREATE (strong identity); a same-named weak entity
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
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from atlas.db.models import Entity, EntityAlias, EntityExternalId
from atlas.ontology.models import Ontology
from atlas.resolution.email_identity import PATTERNS, first_name_pattern, handle_patterns, name_from_local, split_address
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
    method: str = "identifier"                                      # identifier | address_pattern | name | mention


# Internal, source-native identifier for name-only entities: never an ontology
# identity field and never upgrades identity strength.
MENTION_KEY = "source_mention"


class IdentityResolver:
    def __init__(self, session: Session, ontology: Ontology):
        self.session, self.ontology = session, ontology

    def resolve(self, entity_type: str, name: str | None, identifiers: dict[str, str],
                mention_key: str | None = None, properties: dict | None = None) -> Resolution:
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
            if "mailbox" in ids and self.ontology.policies.email_identity.get("link_address_patterns"):
                fits = self._address_pattern_fits(ids["mailbox"], (properties or {}).get("first_name"))
                if len(fits) == 1:
                    return Resolution(outcome=Outcome.MATCHED, entity_id=fits[0], identifiers=ids,
                                      method="address_pattern",
                                      reason=f"address pattern: {ids['mailbox']} fits exactly one person at its organization")
                if fits:
                    return Resolution(outcome=Outcome.CREATE, identifiers=ids, possible_same_as=fits,
                                      reason=f"new identifier; address pattern fits {len(fits)} people")
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

    def _address_pattern_fits(self, mailbox: str, first_name: str | None) -> list[uuid.UUID]:
        """People at the same organization this mailbox could also belong to.
        first.last -> an existing flast / lastname handle, if no OTHER first.last person fits it;
        handle     -> the existing first[.m].last people it fits.
        Never: a handle that is also someone's first name there (john -> john.arnold), a
        lastname handle outside the organization's own (internal) domain, or a display-name
        first name that disagrees."""
        local, org = split_address(mailbox)
        kinds = ["flast", "lastname"] if self._is_internal(org) else ["flast"]
        parsed = name_from_local(local)
        fits: set[uuid.UUID] = set()
        if parsed:
            for handle, kind in handle_patterns(*parsed):
                owner = self._mailbox_owner(f"{handle}@{org}")
                if kind not in kinds or owner is None or not self._first_name_agrees(owner, parsed[0]):
                    continue
                if self._others(PATTERNS[kind](handle, org), owner) or self._others(first_name_pattern(handle, org), owner):
                    continue
                fits.add(owner)
            return sorted(fits)
        if self._others(first_name_pattern(local, org), None):
            return []
        for kind in kinds:
            pattern = PATTERNS[kind](local, org)
            if pattern:
                fits |= set(self._mailboxes_matching(pattern).values())
        return sorted(o for o in fits if first_name is None or self._first_name_agrees(o, first_name))

    def _others(self, pattern: str | None, owner: uuid.UUID | None) -> bool:
        return pattern is not None and any(o != owner for o in self._mailboxes_matching(pattern).values())

    def _is_internal(self, org_domain: str) -> bool:
        org = self.session.execute(select(Entity.properties).join(EntityExternalId, EntityExternalId.entity_id == Entity.id)
                                   .where(EntityExternalId.identifier_type == "domain",
                                          EntityExternalId.value == org_domain)).scalar_one_or_none()
        return bool(org and org.get("is_internal"))

    def _mailbox_owner(self, mailbox: str) -> uuid.UUID | None:
        return self.session.execute(select(EntityExternalId.entity_id).where(
            EntityExternalId.identifier_type == "mailbox", EntityExternalId.value == mailbox)).scalar_one_or_none()

    def _mailboxes_matching(self, pattern: str) -> dict[str, uuid.UUID]:
        rows = self.session.execute(text(
            "SELECT x.value, x.entity_id FROM kg.entity_external_ids x JOIN kg.entities e ON e.id = x.entity_id "
            "WHERE x.identifier_type = 'mailbox' AND x.value ~ :p AND e.status = 'active'"), {"p": pattern})
        return {r.value: r.entity_id for r in rows}

    def _first_name_agrees(self, entity_id: uuid.UUID, first_name: str) -> bool:
        known = (self.session.get(Entity, entity_id).properties or {}).get("first_name")
        return known is None or known.lower() == first_name.lower()

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
