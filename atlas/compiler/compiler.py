"""The deterministic graph compiler.

For one document's candidates, in a fixed order:

  entities:  map type (ontology) -> validate -> resolve identity -> create/match | review
  edges:     both endpoints resolved? -> map relation (ontology) -> validate
             (types, self-edge, evidence) -> cardinality -> confidence policy
             -> commit edge + evidence | review | reject

Only this module decides what enters the graph, and every decision is stored
(candidate_entities / candidate_edges) so any edge can be replayed and explained.
Given identical inputs and ontology, the decisions are identical.
"""

from __future__ import annotations

import uuid
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from atlas.db.models import CandidateEdgeRow, CandidateEntityRow, Entity
from atlas.extraction.candidates import CandidateEdge, CandidateEntity, CandidateSet
from atlas.graph.repository import GraphRepository
from atlas.ingestion.normalized import NormalizedDocument
from atlas.ontology.loader import normalize_label, to_relation_key
from atlas.ontology.mapper import MapStatus, map_entity_type, map_relation
from atlas.ontology.models import Ontology
from atlas.ontology.validator import validate_edge, validate_entity
from atlas.provenance.audit import audit
from atlas.resolution.identity import MENTION_KEY, IdentityResolver, Outcome
from atlas.review.service import ReviewService


class ResolvedEntity(BaseModel):
    local_id: str
    entity_id: uuid.UUID
    entity_type: str


class CompileReport(BaseModel):
    """Serializable summary of one document's compilation."""

    document_id: uuid.UUID
    entities: dict[str, str] = Field(default_factory=dict)   # local_id -> decision
    edges: dict[str, str] = Field(default_factory=dict)      # local_id -> decision
    stats: dict[str, int] = Field(default_factory=dict)


def _count(stats: dict[str, int], key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


class GraphCompiler:
    def __init__(self, session: Session, ontology: Ontology, run_id: uuid.UUID | None = None):
        self.session, self.ontology, self.run_id = session, ontology, run_id
        self.repo = GraphRepository(session, ontology.version, run_id)
        self.reviews = ReviewService(session, ontology.version, run_id)
        self.resolver = IdentityResolver(session, ontology)
        self.acceptance = ontology.policies.acceptance

    def compile(self, doc: NormalizedDocument, candidates: CandidateSet) -> CompileReport:
        report = CompileReport(document_id=doc.document_id)
        resolved: dict[str, ResolvedEntity] = {}
        for cand in sorted(candidates.entities, key=lambda c: c.local_id):
            decision, entity = self._entity(doc, candidates, cand, report)
            report.entities[cand.local_id] = decision
            _count(report.stats, f"entities_{decision.lower()}")
            if entity:
                resolved[cand.local_id] = entity
        for cand in sorted(candidates.edges, key=lambda c: c.local_id):
            decision = self._edge(doc, candidates, cand, resolved, report)
            report.edges[cand.local_id] = decision
            _count(report.stats, f"edges_{decision.lower()}")
        self.session.flush()
        return report

    # --- entities ------------------------------------------------------------------

    def _entity(self, doc: NormalizedDocument, cs: CandidateSet, cand: CandidateEntity,
                report: CompileReport) -> tuple[str, ResolvedEntity | None]:
        def record(decision: str, entity_id=None, mapped_type=None, reason=None):
            self.session.add(CandidateEntityRow(
                id=uuid.uuid4(), ingestion_run_id=self.run_id, document_version_id=cs.document_version_id,
                local_id=cand.local_id, payload=cand.model_dump(mode="json"), mapped_type=mapped_type,
                decision=decision, entity_id=entity_id, reason=reason))
            return decision

        mapping = map_entity_type(self.ontology, cand.suggested_type)
        if mapping.status is not MapStatus.MAPPED:
            label = normalize_label(cand.suggested_type)
            self.reviews.raise_item(
                "NEW_ONTOLOGY_CANDIDATE", f"NEW_ONTOLOGY_CANDIDATE:entity:{label}",
                f"entity type {cand.suggested_type!r} is not in ontology {self.ontology.version}",
                {"kind": "entity_type", "candidate_name": cand.suggested_type},
                source_document_id=doc.document_id, confidence=cand.confidence,
                example={"document_id": str(doc.document_id), "name": cand.name, "source_field": cand.source_field})
            return record("REVIEW", reason="unknown entity type -> NEW_ONTOLOGY_CANDIDATE"), None

        entity_type = mapping.entity_type
        roles = sorted(set(cand.roles) | ({mapping.role} if mapping.role else set()))
        violations = validate_entity(self.ontology, entity_type, cand.name, cand.identifiers, cand.properties, roles)
        if violations:
            self.reviews.raise_item(
                "ONTOLOGY_VIOLATION", f"ONTOLOGY_VIOLATION:entity:{doc.document_id}:{cand.local_id}",
                "; ".join(v.message for v in violations), {"candidate": cand.model_dump(mode="json"),
                                                           "violations": [v.model_dump() for v in violations]},
                source_document_id=doc.document_id)
            return record("REJECTED", mapped_type=entity_type, reason="entity failed validation"), None

        mention_key = None if cand.identifiers else f"{doc.source_system}:{doc.source_external_id}:{cand.local_id}"
        res = self.resolver.resolve(entity_type, cand.name, cand.identifiers, mention_key, cand.properties)
        if res.outcome is Outcome.REVIEW:
            self.reviews.raise_item(
                res.review_type, f"{res.review_type}:{entity_type}:{sorted(res.identifiers.items()) or (cand.name or '').lower()}:"
                                 f"{','.join(map(str, res.candidates))}",
                res.reason, {"entity_type": entity_type, "name": cand.name, "identifiers": res.identifiers},
                source_document_id=doc.document_id, related_entities=res.candidates,
                example={"document_id": str(doc.document_id), "source_field": cand.source_field})
            return record("REVIEW", mapped_type=entity_type, reason=res.reason), None

        name = cand.name or next(iter(res.identifiers.values()), "unnamed")
        if res.outcome is Outcome.CREATE:
            entity_id = self.repo.create_entity(entity_type, name, cand.properties, roles,
                                                "identifier" if res.identifiers else "name_only", doc.document_id)
            decision = "CREATED"
            for other in res.possible_same_as:
                self.reviews.raise_item(
                    "POSSIBLE_DUPLICATE", f"POSSIBLE_DUPLICATE:{min(entity_id, other)}:{max(entity_id, other)}",
                    f"new {entity_type}: {res.reason}",
                    {"entity_type": entity_type, "name": name, "possible_same_as": [str(entity_id), str(other)]},
                    source_document_id=doc.document_id, related_entities=[entity_id, other])
        else:
            entity_id, decision = res.entity_id, "MATCHED"
            had_name = bool(self.session.get(Entity, entity_id).properties.get("first_name"))
            conflicts = self.repo.enrich(entity_id, cand.properties, roles, doc.document_id)
            if res.method == "address_pattern":
                audit(self.session, "identity_linked", "entity", entity_id, self.run_id,
                      {"method": "address_pattern", "identifiers": res.identifiers, "reason": res.reason,
                       "document_id": str(doc.document_id)})
            if not had_name and cand.properties.get("first_name") and cand.name:
                self.repo.rename(entity_id, cand.name, "name learned from a linked address or display name")
            if conflicts:
                self.reviews.raise_item(
                    "CONFLICTING_FACT", f"CONFLICTING_FACT:entity:{entity_id}:{sorted(conflicts)}",
                    f"conflicting property values {sorted(conflicts)}",
                    {"entity_id": str(entity_id), "conflicts": {k: list(v) for k, v in conflicts.items()}},
                    source_document_id=doc.document_id, related_entities=[entity_id],
                    example={"document_id": str(doc.document_id)})
        for id_type, value in res.identifiers.items():
            self.repo.add_identifier(entity_id, id_type, value, doc.source_system)
        if mention_key and decision == "CREATED":
            self.repo.add_identifier(entity_id, MENTION_KEY, mention_key, doc.source_system, strong=False)
        for alias in sorted({a for a in [cand.name, *cand.aliases] if a}):
            self.repo.add_alias(entity_id, alias, doc.document_id)
        stored_type = self.session.get(Entity, entity_id).entity_type
        record(decision, entity_id=entity_id, mapped_type=entity_type, reason=res.reason)
        return decision, ResolvedEntity(local_id=cand.local_id, entity_id=entity_id, entity_type=stored_type)

    # --- edges ------------------------------------------------------------------------

    def _edge(self, doc: NormalizedDocument, cs: CandidateSet, cand: CandidateEdge,
              resolved: dict[str, ResolvedEntity], report: CompileReport) -> str:
        def record(decision: str, mapping=None, violations=None, edge_id=None, reason=None):
            self.session.add(CandidateEdgeRow(
                id=uuid.uuid4(), ingestion_run_id=self.run_id, document_version_id=cs.document_version_id,
                local_id=cand.local_id, payload=cand.model_dump(mode="json"),
                mapping=mapping.model_dump(mode="json") if mapping else None,
                violations=[v.model_dump() for v in violations or []], decision=decision, edge_id=edge_id,
                reason=reason))
            return decision

        src, tgt = resolved.get(cand.source_local_id), resolved.get(cand.target_local_id)
        if src is None or tgt is None:
            # The endpoint's own review item explains why; the edge waits for it.
            return record("HELD", reason="endpoint not resolved (see its review item)")

        mapping = map_relation(self.ontology, cand.suggested_relation, src.entity_type, tgt.entity_type)
        example = {"document_id": str(doc.document_id), "source_field": cand.evidence.source_field,
                   "evidence_text": cand.evidence.evidence_text}
        if mapping.status is MapStatus.UNRESOLVED:
            key = to_relation_key(cand.suggested_relation)
            self.reviews.raise_item(
                "NEW_ONTOLOGY_CANDIDATE", f"NEW_ONTOLOGY_CANDIDATE:relation:{key}:{src.entity_type}:{tgt.entity_type}",
                mapping.reason, {"kind": "relation", "suggested_relation": key, "source_type": src.entity_type,
                                 "target_type": tgt.entity_type},
                source_document_id=doc.document_id, confidence=cand.confidence, example=example)
            return record("REVIEW", mapping=mapping, reason="no canonical relation -> NEW_ONTOLOGY_CANDIDATE")
        if mapping.status is MapStatus.TYPE_VIOLATION:
            self.reviews.raise_item(
                "ONTOLOGY_VIOLATION", f"ONTOLOGY_VIOLATION:{mapping.canonical}:{src.entity_type}:{tgt.entity_type}",
                mapping.reason, {"relation": mapping.canonical, "source_type": src.entity_type,
                                 "target_type": tgt.entity_type},
                source_document_id=doc.document_id, example=example)
            return record("REJECTED", mapping=mapping, reason=mapping.reason)

        relation = mapping.canonical
        violations = validate_edge(self.ontology, relation, src.entity_type, tgt.entity_type,
                                   src.entity_id == tgt.entity_id, cand.provenance_class, cand.evidence)
        if violations:
            review_type = "MISSING_EVIDENCE" if any(v.code == "MISSING_EVIDENCE" for v in violations) else "ONTOLOGY_VIOLATION"
            if not all(v.code == "SELF_EDGE" for v in violations):
                self.reviews.raise_item(
                    review_type, f"{review_type}:{relation}:{src.entity_id}:{tgt.entity_id}",
                    "; ".join(v.message for v in violations),
                    {"relation": relation, "source": str(src.entity_id), "target": str(tgt.entity_id)},
                    source_document_id=doc.document_id, related_entities=[src.entity_id, tgt.entity_id],
                    example=example)
            return record("REJECTED", mapping=mapping, violations=violations, reason="edge failed validation")

        rel_def = self.ontology.relations[relation]
        if rel_def.max_targets_per_source:
            others = [t for t in self.repo.targets_of(src.entity_id, relation) if t != tgt.entity_id]
            if len(others) >= rel_def.max_targets_per_source:
                self.reviews.raise_item(
                    "CONFLICTING_FACT", f"CONFLICTING_FACT:{relation}:{src.entity_id}",
                    f"{relation} allows {rel_def.max_targets_per_source} target(s); another already exists",
                    {"relation": relation, "source": str(src.entity_id), "existing": [str(o) for o in others],
                     "proposed": str(tgt.entity_id)},
                    source_document_id=doc.document_id, related_entities=[src.entity_id, tgt.entity_id, *others],
                    example=example)
                return record("REVIEW", mapping=mapping, reason="cardinality conflict")

        threshold = self.acceptance.get(cand.provenance_class, 1.01)
        if cand.confidence < threshold:
            self.reviews.raise_item(
                "LOW_CONFIDENCE_RELATION", f"LOW_CONFIDENCE_RELATION:{relation}:{src.entity_id}:{tgt.entity_id}",
                f"confidence {cand.confidence:.2f} < {threshold:.2f} for {cand.provenance_class}",
                {"relation": relation, "source": str(src.entity_id), "target": str(tgt.entity_id)},
                source_document_id=doc.document_id, related_entities=[src.entity_id, tgt.entity_id],
                confidence=cand.confidence, example=example)
            return record("REVIEW", mapping=mapping, reason="below acceptance threshold")

        edge_id, _ = self.repo.upsert_edge(src.entity_id, relation, tgt.entity_id, cand.provenance_class,
                                           cand.confidence, cand.evidence.observed_at, cand.properties)
        self.repo.attach_evidence(edge_id, cand.evidence, cand.provenance_class, cand.extractor,
                                  cand.extractor_version, cand.confidence)
        for role, entity in ((rel_def.implies_source_role, src), (rel_def.implies_target_role, tgt)):
            if role:
                self.repo.enrich(entity.entity_id, {}, [role])
        return record("ACCEPTED", mapping=mapping, edge_id=edge_id, reason=f"{mapping.method} mapping; policy passed")
