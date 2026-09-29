"""Write-side persistence for the knowledge graph.

All "does this already exist?" questions are answered by database constraints
(ON CONFLICT), so re-running the pipeline cannot create duplicate aliases,
mentions, edges or evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from brain_v0.db.models import (
    Entity,
    EntityAlias,
    EntityMention,
    Relationship,
    RelationshipEvidence,
    ResolutionLog,
)
from brain_v0.normalize import normalize_name


@dataclass(frozen=True)
class EntityRow:
    id: uuid.UUID
    canonical_name: str
    entity_type: str


class GraphRepository:
    def __init__(self, session: Session):
        self.session = session

    # --- entity lookups (used by the resolver) --------------------------------

    def entities_by_normalized_name(self, normalized: str) -> list[EntityRow]:
        rows = self.session.execute(
            select(Entity.id, Entity.canonical_name, Entity.entity_type)
            .where(Entity.normalized_name == normalized)
            .order_by(Entity.created_at, Entity.id)
        )
        return [EntityRow(*r) for r in rows]

    def entities_by_surface_forms(self, normalized_forms: list[str]) -> list[tuple[EntityRow, str]]:
        """Entities whose canonical name or any alias equals one of the forms.
        Returns (entity, matched_form) pairs."""
        if not normalized_forms:
            return []
        rows = self.session.execute(
            text(
                """
                SELECT e.id, e.canonical_name, e.entity_type, a.normalized_alias AS form
                FROM entity_aliases a JOIN entities e ON e.id = a.entity_id
                WHERE a.normalized_alias = ANY(:forms)
                UNION
                SELECT e.id, e.canonical_name, e.entity_type, e.normalized_name AS form
                FROM entities e WHERE e.normalized_name = ANY(:forms)
                """
            ),
            {"forms": normalized_forms},
        )
        return [(EntityRow(r.id, r.canonical_name, r.entity_type), r.form) for r in rows]

    def fuzzy_candidates(self, normalized: str, limit: int = 10) -> list[tuple[EntityRow, str]]:
        """Trigram-similar canonical names and aliases (pg_trgm), for the fuzzy step."""
        rows = self.session.execute(
            text(
                """
                SELECT e.id, e.canonical_name, e.entity_type, a.normalized_alias AS form,
                       similarity(a.normalized_alias, :n) AS sim
                FROM entity_aliases a JOIN entities e ON e.id = a.entity_id
                WHERE similarity(a.normalized_alias, :n) > 0.3
                ORDER BY sim DESC LIMIT :limit
                """
            ),
            {"n": normalized, "limit": limit},
        )
        return [(EntityRow(r.id, r.canonical_name, r.entity_type), r.form) for r in rows]

    def compact_matches(self, compact: str) -> list[tuple[EntityRow, str]]:
        rows = self.session.execute(
            text(
                """
                SELECT e.id, e.canonical_name, e.entity_type, a.normalized_alias AS form
                FROM entity_aliases a JOIN entities e ON e.id = a.entity_id
                WHERE replace(a.normalized_alias, ' ', '') = :c
                """
            ),
            {"c": compact},
        )
        return [(EntityRow(r.id, r.canonical_name, r.entity_type), r.form) for r in rows]

    def nearest_by_embedding(self, embedding: list[float], limit: int = 5) -> list[tuple[EntityRow, float]]:
        rows = self.session.execute(
            select(
                Entity.id,
                Entity.canonical_name,
                Entity.entity_type,
                (1 - Entity.embedding.cosine_distance(embedding)).label("sim"),
            )
            .where(Entity.embedding.is_not(None))
            .order_by(Entity.embedding.cosine_distance(embedding))
            .limit(limit)
        )
        return [(EntityRow(r.id, r.canonical_name, r.entity_type), float(r.sim)) for r in rows]

    def surface_forms(self, entity_id: uuid.UUID) -> list[str]:
        """Canonical name plus every alias (display form)."""
        rows = self.session.execute(
            select(EntityAlias.alias).where(EntityAlias.entity_id == entity_id)
        ).scalars()
        name = self.session.get(Entity, entity_id).canonical_name
        return list(dict.fromkeys([name, *rows]))

    # --- entity writes ------------------------------------------------------------

    def create_entity(
        self,
        name: str,
        entity_type: str,
        description: str | None,
        properties: dict | None = None,
        embedding: list[float] | None = None,
    ) -> uuid.UUID:
        entity = Entity(
            id=uuid.uuid4(),
            canonical_name=name,
            normalized_name=normalize_name(name),
            entity_type=entity_type,
            description=description,
            properties={"type_votes": {entity_type: 1}, **(properties or {})},
            embedding=embedding,
        )
        self.session.add(entity)
        self.session.flush()
        self.add_alias(entity.id, name, source="name")
        return entity.id

    def add_alias(self, entity_id: uuid.UUID, alias: str, source: str) -> bool:
        normalized = normalize_name(alias)
        if not normalized:
            return False
        result = self.session.execute(
            insert(EntityAlias)
            .values(id=uuid.uuid4(), entity_id=entity_id, alias=alias,
                    normalized_alias=normalized, source=source)
            .on_conflict_do_nothing(index_elements=["entity_id", "normalized_alias"])
            .returning(EntityAlias.id)
        )
        return result.scalar_one_or_none() is not None

    def alias_owners(self, normalized_alias: str) -> set[uuid.UUID]:
        return set(
            self.session.execute(
                select(EntityAlias.entity_id).where(EntityAlias.normalized_alias == normalized_alias)
            ).scalars()
        )

    def vote_entity_type(self, entity_id: uuid.UUID, entity_type: str) -> str:
        """Record the type this extraction proposed; the entity keeps the
        majority type (ties keep the current one). Returns the resulting type."""
        entity = self.session.get(Entity, entity_id)
        votes = dict(entity.properties.get("type_votes", {}))
        votes[entity_type] = votes.get(entity_type, 0) + 1
        best = max(votes, key=lambda t: (votes[t], t == entity.entity_type))
        entity.properties = {**entity.properties, "type_votes": votes}
        entity.entity_type = best
        return best

    def fill_description(self, entity_id: uuid.UUID, description: str | None) -> None:
        if description:
            self.session.execute(
                update(Entity)
                .where(Entity.id == entity_id, Entity.description.is_(None))
                .values(description=description)
            )

    def add_mention(
        self,
        entity_id: uuid.UUID,
        chunk_id: uuid.UUID,
        mention_text: str,
        start: int | None,
        end: int | None,
        confidence: float | None,
        run_id: uuid.UUID | None,
    ) -> None:
        self.session.execute(
            insert(EntityMention)
            .values(id=uuid.uuid4(), entity_id=entity_id, chunk_id=chunk_id, mention_text=mention_text,
                    start_offset=start, end_offset=end, confidence=confidence, extraction_run_id=run_id)
            .on_conflict_do_nothing(index_elements=["entity_id", "chunk_id", "mention_text"])
        )

    # --- relationship writes -------------------------------------------------------

    def upsert_relationship(
        self, source_id: uuid.UUID, target_id: uuid.UUID, relationship_type: str, description: str | None
    ) -> tuple[uuid.UUID, bool]:
        """Return (relationship_id, created)."""
        new_id = uuid.uuid4()
        inserted = self.session.execute(
            insert(Relationship)
            .values(id=new_id, source_entity_id=source_id, target_entity_id=target_id,
                    relationship_type=relationship_type, description=description, properties={})
            .on_conflict_do_nothing(constraint="uq_relationship_edge")
            .returning(Relationship.id)
        ).scalar_one_or_none()
        if inserted:
            return inserted, True
        existing = self.session.execute(
            select(Relationship.id).where(
                Relationship.source_entity_id == source_id,
                Relationship.target_entity_id == target_id,
                Relationship.relationship_type == relationship_type,
            )
        ).scalar_one()
        return existing, False

    def attach_evidence(
        self,
        relationship_id: uuid.UUID,
        chunk_id: uuid.UUID,
        evidence_text: str,
        confidence: float | None,
        run_id: uuid.UUID | None,
        details: dict,
    ) -> bool:
        """Attach evidence (one row per edge+chunk) and refresh the edge's
        support count / confidence. Returns False if this chunk already
        supported the edge."""
        inserted = self.session.execute(
            insert(RelationshipEvidence)
            .values(id=uuid.uuid4(), relationship_id=relationship_id, chunk_id=chunk_id,
                    evidence_text=evidence_text, confidence=confidence,
                    extraction_run_id=run_id, details=details)
            .on_conflict_do_nothing(index_elements=["relationship_id", "chunk_id"])
            .returning(RelationshipEvidence.id)
        ).scalar_one_or_none() is not None
        if inserted:
            self.refresh_support(relationship_id)
        return inserted

    def refresh_support(self, relationship_id: uuid.UUID) -> None:
        count, best = self.session.execute(
            select(func.count(), func.max(RelationshipEvidence.confidence)).where(
                RelationshipEvidence.relationship_id == relationship_id
            )
        ).one()
        self.session.execute(
            update(Relationship)
            .where(Relationship.id == relationship_id)
            .values(support_count=count, confidence=best, updated_at=func.now())
        )

    def prune_unsupported(self) -> dict[str, int]:
        """Invariant: no edge without evidence, no entity without a mention.
        Only matters after documents change and their chunks are deleted."""
        edges = self.session.execute(
            text("DELETE FROM relationships r WHERE NOT EXISTS "
                 "(SELECT 1 FROM relationship_evidence e WHERE e.relationship_id = r.id)")
        ).rowcount
        self.session.execute(
            text("UPDATE relationships r SET support_count = "
                 "(SELECT count(*) FROM relationship_evidence e WHERE e.relationship_id = r.id)")
        )
        entities = self.session.execute(
            text("DELETE FROM entities n WHERE NOT EXISTS "
                 "(SELECT 1 FROM entity_mentions m WHERE m.entity_id = n.id)")
        ).rowcount
        return {"relationships_pruned": edges, "entities_pruned": entities}

    # --- audit log --------------------------------------------------------------------

    def log(self, *, kind: str, decision: str, subject: str, run_id=None, chunk_id=None,
            method=None, score=None, entity_id=None, relationship_id=None, details=None) -> None:
        self.session.add(ResolutionLog(
            id=uuid.uuid4(), extraction_run_id=run_id, chunk_id=chunk_id, kind=kind,
            decision=decision, method=method, score=score, subject=subject,
            entity_id=entity_id, relationship_id=relationship_id, details=details or {},
        ))
