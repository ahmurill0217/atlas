"""The only code that writes canonical graph objects. Called by the compiler
after validation; every write is idempotent and audited."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from atlas.db.models import DocumentSection, Edge, EdgeEvidence, Entity, EntityAlias, EntityExternalId
from atlas.extraction.candidates import EvidenceRef
from atlas.provenance.audit import audit
from atlas.provenance.evidence import evidence_key, stronger
from atlas.resolution.normalize import normalize_name


class GraphRepository:
    def __init__(self, session: Session, ontology_version: str, run_id: uuid.UUID | None = None):
        self.session, self.ontology_version, self.run_id = session, ontology_version, run_id

    # --- entities -------------------------------------------------------------------

    def create_entity(self, entity_type: str, name: str, properties: dict, roles: list[str],
                      identity_strength: str) -> uuid.UUID:
        entity = Entity(id=uuid.uuid4(), entity_type=entity_type, canonical_name=name,
                        normalized_name=normalize_name(name), ontology_version=self.ontology_version,
                        properties=properties, roles=sorted(set(roles)), identity_strength=identity_strength)
        self.session.add(entity)
        self.session.flush()
        audit(self.session, "entity_created", "entity", entity.id, self.run_id,
              {"entity_type": entity_type, "canonical_name": name, "identity_strength": identity_strength})
        return entity.id

    def add_identifier(self, entity_id: uuid.UUID, identifier_type: str, value: str, source_system: str | None,
                       strong: bool = True) -> bool:
        """False if the identifier already belongs to another entity (never reassigned silently).
        `strong=False` records a lookup key that does not upgrade identity strength."""
        inserted = self.session.execute(
            insert(EntityExternalId).values(id=uuid.uuid4(), entity_id=entity_id, identifier_type=identifier_type,
                                            value=value, source_system=source_system)
            .on_conflict_do_nothing(index_elements=["identifier_type", "value"])
            .returning(EntityExternalId.entity_id)).scalar_one_or_none()
        if inserted:
            if strong and self.session.get(Entity, entity_id).identity_strength != "identifier":
                self.session.execute(update(Entity).where(Entity.id == entity_id).values(identity_strength="identifier"))
            return True
        owner = self.session.execute(select(EntityExternalId.entity_id).where(
            EntityExternalId.identifier_type == identifier_type, EntityExternalId.value == value)).scalar_one()
        return owner == entity_id

    def add_alias(self, entity_id: uuid.UUID, alias: str, source_document_id: uuid.UUID | None) -> None:
        normalized = normalize_name(alias)
        if normalized:
            self.session.execute(
                insert(EntityAlias).values(id=uuid.uuid4(), entity_id=entity_id, alias=alias,
                                           normalized_alias=normalized, source_document_id=source_document_id)
                .on_conflict_do_nothing(index_elements=["entity_id", "normalized_alias"]))

    def enrich(self, entity_id: uuid.UUID, properties: dict, roles: list[str]) -> dict[str, tuple]:
        """Fill properties the entity lacks and add roles. Never overwrites a
        different existing value: returns {property: (existing, proposed)} conflicts."""
        entity = self.session.get(Entity, entity_id)
        merged, conflicts = dict(entity.properties), {}
        for key, value in properties.items():
            if key not in merged or merged[key] in (None, ""):
                merged[key] = value
            elif merged[key] != value:
                conflicts[key] = (merged[key], value)
        added_roles = sorted(set(roles) - set(entity.roles))
        if merged != entity.properties or added_roles:
            entity.properties, entity.roles = merged, sorted(set(entity.roles) | set(roles))
            if added_roles:
                audit(self.session, "role_added", "entity", entity_id, self.run_id, {"roles": added_roles})
        return conflicts

    # --- edges ------------------------------------------------------------------------

    def targets_of(self, source_id: uuid.UUID, relation: str) -> list[uuid.UUID]:
        return list(self.session.execute(select(Edge.target_entity_id).where(
            Edge.source_entity_id == source_id, Edge.relation_type == relation, Edge.status == "active")).scalars())

    def upsert_edge(self, source_id: uuid.UUID, relation: str, target_id: uuid.UUID, provenance_class: str,
                    confidence: float, observed_at: datetime | None,
                    properties: dict | None = None) -> tuple[uuid.UUID, bool]:
        new_id = uuid.uuid4()
        created = self.session.execute(
            insert(Edge).values(id=new_id, source_entity_id=source_id, relation_type=relation,
                                target_entity_id=target_id, ontology_version=self.ontology_version,
                                provenance_class=provenance_class, confidence=confidence,
                                properties=properties or {},
                                first_seen_at=observed_at, last_seen_at=observed_at)
            .on_conflict_do_nothing(constraint="uq_kg_edge").returning(Edge.id)).scalar_one_or_none()
        if created:
            audit(self.session, "edge_created", "edge", created, self.run_id,
                  {"relation": relation, "source": str(source_id), "target": str(target_id)})
            return created, True
        edge = self.session.execute(select(Edge).where(
            Edge.source_entity_id == source_id, Edge.relation_type == relation,
            Edge.target_entity_id == target_id)).scalar_one()
        edge.provenance_class = stronger(edge.provenance_class, provenance_class)
        missing = {k: v for k, v in (properties or {}).items() if k not in edge.properties}
        if missing:
            edge.properties = {**edge.properties, **missing}
        edge.confidence = max(edge.confidence or 0.0, confidence)
        if observed_at:
            edge.first_seen_at = min(filter(None, [edge.first_seen_at, observed_at]))
            edge.last_seen_at = max(filter(None, [edge.last_seen_at, observed_at]))
        return edge.id, False

    def section_id(self, document_version_id: uuid.UUID | None, ordinal: int | None) -> uuid.UUID | None:
        if document_version_id is None or ordinal is None:
            return None
        return self.session.execute(select(DocumentSection.id).where(
            DocumentSection.document_version_id == document_version_id,
            DocumentSection.ordinal == ordinal)).scalar_one_or_none()

    def attach_evidence(self, edge_id: uuid.UUID, ev: EvidenceRef, provenance_class: str, extractor: str,
                        extractor_version: str, confidence: float) -> bool:
        section_id = self.section_id(ev.document_version_id, ev.section_ordinal)
        inserted = self.session.execute(
            insert(EdgeEvidence).values(
                id=uuid.uuid4(), edge_id=edge_id, evidence_key=evidence_key(ev), document_id=ev.document_id,
                document_version_id=ev.document_version_id, section_id=section_id,
                provenance_class=provenance_class, source_field=ev.source_field, evidence_text=ev.evidence_text,
                start_offset=ev.start_offset, end_offset=ev.end_offset, page_number=ev.page_number,
                observed_at=ev.observed_at, extractor=extractor, extractor_version=extractor_version,
                confidence=confidence)
            .on_conflict_do_nothing(index_elements=["edge_id", "evidence_key"]).returning(EdgeEvidence.id)
        ).scalar_one_or_none()
        if inserted:
            self.session.execute(update(Edge).where(Edge.id == edge_id).values(updated_at=func.now()))
        return inserted is not None
