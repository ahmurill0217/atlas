"""A document's contribution to the graph is replaced as a whole each time it is processed.

The graph holds only what the current version of each document supports; no history is
kept. After a version is compiled, everything the document supported before that this
compile did not produce again is removed; a deleted document is the same with nothing
produced:

  candidate rows  the document's earlier rows (older versions, earlier processing) go
  evidence        its evidence rows this compile did not write again go
  edges           an edge left without evidence goes; otherwise first/last seen,
                  confidence and provenance are recomputed from the evidence left
  versions        older versions go, with their sections
  review items    the document's examples are taken out before compiling (a still-current
                  issue is raised again with fresh ones); an open item it raised that ends
                  with no examples and was not raised again is closed as SOURCE_REMOVED
  entities        an entity that lost a reference goes when no current document version
                  references it, no edge touches it, and no human decision (merge history,
                  a resolved review item) names it; otherwise its aliases, roles and the
                  properties this document had set are derived again from what references it

Every step is idempotent, and the audit entries carry ids and counts, never content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from atlas.db.models import (
    CandidateEdgeRow,
    CandidateEntityRow,
    DocumentVersion,
    Edge,
    EdgeEvidence,
    Entity,
    EntityAlias,
    IndexQueue,
    ReviewItem,
)
from atlas.ontology.mapper import MapStatus, map_entity_type
from atlas.ontology.models import Ontology
from atlas.provenance.audit import audit
from atlas.provenance.evidence import PROVENANCE_STRENGTH
from atlas.resolution.normalize import normalize_name

REFERENCING = ("CREATED", "MATCHED")                 # candidate decisions that reference an entity
HUMAN_DECISIONS = ("APPROVED", "REJECTED", "DEFERRED")
SOURCE_REMOVED = "SOURCE_REMOVED"


@dataclass
class Prior:
    """What a document contributed before this processing."""
    entity_rows: list[uuid.UUID] = field(default_factory=list)
    edge_rows: list[uuid.UUID] = field(default_factory=list)
    entities: set[uuid.UUID] = field(default_factory=set)
    reviews: set[uuid.UUID] = field(default_factory=set)


def lock_document(session: Session, document_id: uuid.UUID) -> None:
    """One writer per document until the transaction ends (concurrent workers, task retries)."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": str(document_id)})


def enqueue_index(session: Session, document_id: uuid.UUID, action: str) -> None:
    """Record the search-index action the document now needs; the latest action wins."""
    session.execute(insert(IndexQueue).values(document_id=document_id, action=action)
                    .on_conflict_do_update(index_elements=["document_id"],
                                           set_={"action": action, "enqueued_at": func.now()}))


class DocumentSweep:
    def __init__(self, session: Session, ontology: Ontology, run_id: uuid.UUID | None = None):
        self.session, self.ontology, self.run_id = session, ontology, run_id
        self.stats: dict[str, int] = {}

    def _count(self, key: str, n: int = 1) -> None:
        if n:
            self.stats[key] = self.stats.get(key, 0) + n

    # --- before compiling ---------------------------------------------------------------

    def begin(self, document_id: uuid.UUID, spare_version: uuid.UUID | None = None,
              scrub_examples: bool = True) -> Prior:
        """Note the document's candidate rows (except `spare_version`'s) and take its examples
        out of review items."""
        def rows(model):
            q = (select(model.id, *([model.entity_id, model.decision] if model is CandidateEntityRow else []))
                 .join(DocumentVersion, DocumentVersion.id == model.document_version_id)
                 .where(DocumentVersion.document_id == document_id))
            if spare_version:
                q = q.where(model.document_version_id != spare_version)
            return self.session.execute(q).all()

        entity_rows = rows(CandidateEntityRow)
        prior = Prior(entity_rows=[r.id for r in entity_rows], edge_rows=[r.id for r in rows(CandidateEdgeRow)],
                      entities={r.entity_id for r in entity_rows if r.decision in REFERENCING and r.entity_id})
        if scrub_examples:
            for item in self.session.execute(select(ReviewItem).where(
                    ReviewItem.examples.contains([{"document_id": str(document_id)}]))).scalars():
                item.examples = [x for x in item.examples if x.get("document_id") != str(document_id)]
                prior.reviews.add(item.id)
        return prior

    # --- after compiling ----------------------------------------------------------------

    def finish(self, document_id: uuid.UUID, prior: Prior, keep_version: uuid.UUID | None = None,
               kept_evidence: set[tuple[uuid.UUID, str]] | frozenset = frozenset(),
               raised: set[str] | frozenset = frozenset()) -> dict[str, int]:
        """Remove what the document supported before and no longer does. With no `keep_version`
        every version goes (the document is being deleted)."""
        affected = set(prior.entities)
        if prior.entity_rows:
            self.session.execute(delete(CandidateEntityRow).where(CandidateEntityRow.id.in_(prior.entity_rows)))
        if prior.edge_rows:
            self.session.execute(delete(CandidateEdgeRow).where(CandidateEdgeRow.id.in_(prior.edge_rows)))

        evidence = self.session.execute(select(EdgeEvidence.id, EdgeEvidence.edge_id, EdgeEvidence.evidence_key)
                                        .where(EdgeEvidence.document_id == document_id)).all()
        stale = [r for r in evidence if (r.edge_id, r.evidence_key) not in kept_evidence]
        if stale:
            self.session.execute(delete(EdgeEvidence).where(EdgeEvidence.id.in_([r.id for r in stale])))
        self._count("evidence_removed", len(stale))

        old = delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
        if keep_version:
            old = old.where(DocumentVersion.id != keep_version)
        self._count("versions_removed", self.session.execute(old).rowcount)

        for edge_id in sorted({r.edge_id for r in stale}):
            affected |= self._settle_edge(edge_id)
        self._close_reviews(document_id, prior.reviews, raised)
        for entity_id in sorted(affected):
            self._settle_entity(entity_id)
        self.session.flush()
        return self.stats

    def _settle_edge(self, edge_id: uuid.UUID) -> set[uuid.UUID]:
        """Delete an edge with no evidence left (returns its endpoints); else recompute it."""
        edge = self.session.get(Edge, edge_id)
        if edge is None:
            return set()
        n, first, last, confidence = self.session.execute(
            select(func.count(), func.min(EdgeEvidence.observed_at), func.max(EdgeEvidence.observed_at),
                   func.max(EdgeEvidence.confidence)).where(EdgeEvidence.edge_id == edge_id)).one()
        if n == 0:
            ends = {edge.source_entity_id, edge.target_entity_id}
            audit(self.session, "edge_deleted", "edge", edge_id, self.run_id,
                  {"relation": edge.relation_type, "reason": "no evidence left"})
            self.session.delete(edge)
            self.session.flush()
            self._count("edges_removed")
            return ends
        classes = self.session.execute(select(EdgeEvidence.provenance_class).distinct()
                                       .where(EdgeEvidence.edge_id == edge_id)).scalars().all()
        edge.first_seen_at, edge.last_seen_at, edge.confidence = first, last, confidence
        edge.provenance_class = max(classes, key=lambda c: PROVENANCE_STRENGTH.get(c, 0))
        return set()

    def _close_reviews(self, document_id: uuid.UUID, touched: set[uuid.UUID], raised: set[str]) -> None:
        items = self.session.execute(select(ReviewItem).where(
            ReviewItem.status == "OPEN",
            or_(ReviewItem.id.in_(touched or [uuid.UUID(int=0)]), ReviewItem.source_document_id == document_id))
        ).scalars().all()
        for item in items:
            if item.dedupe_key in raised or item.examples:
                continue
            item.status, item.reviewer, item.resolved_at = SOURCE_REMOVED, "pipeline", func.now()
            item.resolution = {"decision": SOURCE_REMOVED, "document_id": str(document_id),
                               "note": "the document that raised it was changed or deleted and no longer does"}
            audit(self.session, "review_source_removed", "review_item", item.id, self.run_id,
                  {"document_id": str(document_id)})
            self._count("reviews_closed")

    # --- entities -----------------------------------------------------------------------

    def _settle_entity(self, entity_id: uuid.UUID) -> None:
        entity = self.session.get(Entity, entity_id)
        if entity is None:
            return
        refs = self.session.execute(
            select(CandidateEntityRow.payload, DocumentVersion.document_id)
            .join(DocumentVersion, DocumentVersion.id == CandidateEntityRow.document_version_id)
            .where(CandidateEntityRow.entity_id == entity_id, CandidateEntityRow.decision.in_(REFERENCING))
            .order_by(CandidateEntityRow.created_at, CandidateEntityRow.id)).all()
        edges = self.session.execute(select(Edge.relation_type, Edge.source_entity_id).where(
            or_(Edge.source_entity_id == entity_id, Edge.target_entity_id == entity_id))).all()
        if not refs and not edges and not self._human_decision(entity_id):
            audit(self.session, "entity_deleted", "entity", entity_id, self.run_id,
                  {"entity_type": entity.entity_type, "reason": "no document references it"})
            self.session.delete(entity)
            self.session.flush()
            self._count("entities_removed")
            return
        if refs:
            self._derive_again(entity, refs, edges)

    def _human_decision(self, entity_id: uuid.UUID) -> bool:
        return bool(self.session.execute(text(
            "SELECT EXISTS (SELECT 1 FROM kg.entity_merge_history WHERE :e IN (source_entity_id, target_entity_id))"
            " OR EXISTS (SELECT 1 FROM kg.review_items WHERE status = ANY(:s) AND :e = ANY(related_entities))"),
            {"e": entity_id, "s": list(HUMAN_DECISIONS)}).scalar())

    def _derive_again(self, entity: Entity, refs, edges) -> None:
        """Aliases, roles and properties from the documents that still reference the entity."""
        payloads = [r.payload for r in refs]
        names = {normalize_name(n) for p in payloads for n in [p.get("name"), *(p.get("aliases") or [])] if n}
        gone = self.session.execute(delete(EntityAlias).where(
            EntityAlias.entity_id == entity.id, EntityAlias.normalized_alias.not_in(names or {""}))).rowcount
        self._count("aliases_removed", gone)

        roles = set()
        for p in payloads:
            roles |= set(p.get("roles") or [])
            mapping = map_entity_type(self.ontology, p.get("suggested_type") or "")
            if mapping.status is MapStatus.MAPPED and mapping.role:
                roles.add(mapping.role)
        for relation, source_id in edges:
            rel = self.ontology.relations.get(relation)
            if rel:
                roles |= {r for r in [rel.implies_source_role if source_id == entity.id else None,
                                      rel.implies_target_role if source_id != entity.id else None] if r}
        if roles != set(entity.roles):
            audit(self.session, "roles_derived_again", "entity", entity.id, self.run_id,
                  {"removed": sorted(set(entity.roles) - roles), "added": sorted(roles - set(entity.roles))})
            entity.roles = sorted(roles)

        by_doc: dict[str, dict] = {}
        for r in refs:
            for key, value in (r.payload.get("properties") or {}).items():
                by_doc.setdefault(str(r.document_id), {}).setdefault(key, value)
        properties, sources, dropped = dict(entity.properties), dict(entity.property_sources or {}), []
        for key, source in list(sources.items()):
            if key in by_doc.get(source, {}):
                continue                                   # the document that set it still states it
            properties.pop(key, None)
            sources.pop(key)
            dropped.append(key)
            for doc, values in by_doc.items():             # refs are in processing order: first one wins
                if values.get(key) not in (None, ""):
                    properties[key], sources[key] = values[key], doc
                    break
        if dropped:
            audit(self.session, "properties_derived_again", "entity", entity.id, self.run_id,
                  {"properties": sorted(dropped)})
            entity.properties, entity.property_sources = properties, sources
