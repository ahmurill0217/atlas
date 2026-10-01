"""First-class review queue.

The same issue raised again (same dedupe key) increments `frequency` and
collects a few examples instead of creating a new item, so recurring gaps such
as a missing ontology concept surface as one ranked item, not thousands.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from atlas.db.models import ReviewItem
from atlas.ontology.mapper import MapStatus, map_entity_type, map_relation
from atlas.ontology.models import Ontology
from atlas.provenance.audit import audit

REVIEW_TYPES = {
    "UNKNOWN_ENTITY_TYPE", "UNKNOWN_RELATION_TYPE", "AMBIGUOUS_ENTITY_MATCH", "POSSIBLE_DUPLICATE",
    "LOW_CONFIDENCE_RELATION", "ONTOLOGY_VIOLATION", "CONFLICTING_FACT", "MISSING_EVIDENCE",
    "NEW_ONTOLOGY_CANDIDATE",
}
MAX_EXAMPLES = 5


class ReviewService:
    def __init__(self, session: Session, ontology_version: str, run_id: uuid.UUID | None = None):
        self.session, self.ontology_version, self.run_id = session, ontology_version, run_id
        self.raised: set[str] = set()                              # dedupe keys raised through this service

    def raise_item(self, review_type: str, dedupe_key: str, reason: str, payload: dict,
                   source_document_id: uuid.UUID | None = None, related_entities: list[uuid.UUID] | None = None,
                   confidence: float | None = None, example: dict | None = None) -> uuid.UUID:
        if review_type not in REVIEW_TYPES:
            raise ValueError(f"unknown review type {review_type}")
        self.raised.add(dedupe_key)
        new_id = uuid.uuid4()
        inserted = self.session.execute(
            insert(ReviewItem).values(
                id=new_id, review_type=review_type, dedupe_key=dedupe_key, reason=reason,
                candidate_payload=payload, source_document_id=source_document_id,
                related_entities=related_entities or [], confidence=confidence,
                ontology_version=self.ontology_version, examples=[example] if example else [])
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(ReviewItem.id)
        ).scalar_one_or_none()
        if inserted:
            audit(self.session, "review_created", "review_item", inserted, self.run_id,
                  {"review_type": review_type, "dedupe_key": dedupe_key})
            return inserted
        item = self.session.execute(select(ReviewItem).where(ReviewItem.dedupe_key == dedupe_key)).scalar_one()
        if item.status == "SOURCE_REMOVED":                # closed when its document went; a document raises it again
            item.status, item.reviewer, item.resolved_at, item.resolution = "OPEN", None, None, None
            audit(self.session, "review_reopened", "review_item", item.id, self.run_id, {"dedupe_key": dedupe_key})
        examples = list(item.examples)
        if example and example not in examples and len(examples) < MAX_EXAMPLES:
            examples.append(example)
        if example is None or example not in item.examples:
            self.session.execute(update(ReviewItem).where(ReviewItem.id == item.id)
                                 .values(frequency=ReviewItem.frequency + 1, examples=examples,
                                         updated_at=func.now()))
        return item.id


def reconcile_with_ontology(session: Session, ontology: Ontology, run_id: uuid.UUID | None = None) -> int:
    """After a new ontology version is registered, close NEW_ONTOLOGY_CANDIDATE
    items it now covers (AUTO_RESOLVED, audited). The facts themselves are
    created when documents are reprocessed under the new version."""
    resolved = 0
    items = session.execute(select(ReviewItem).where(
        ReviewItem.review_type == "NEW_ONTOLOGY_CANDIDATE", ReviewItem.status == "OPEN")).scalars().all()
    for item in items:
        p = item.candidate_payload
        if p.get("kind") == "entity_type":
            m = map_entity_type(ontology, p["candidate_name"])
            mapped_to = m.entity_type if m.status is MapStatus.MAPPED else None
        else:
            m = map_relation(ontology, p["suggested_relation"], p["source_type"], p["target_type"])
            mapped_to = m.canonical if m.status is MapStatus.MAPPED else None
        if mapped_to:
            item.status, item.resolved_at, item.reviewer = "AUTO_RESOLVED", func.now(), "ontology"
            item.resolution = {"ontology_version": ontology.version, "mapped_to": mapped_to}
            audit(session, "review_auto_resolved", "review_item", item.id, run_id,
                  {"ontology_version": ontology.version, "mapped_to": mapped_to})
            resolved += 1
    return resolved


RESOLUTION_STATUSES = {"APPROVED", "REJECTED", "DEFERRED"}


def resolve_review(session: Session, review_id: uuid.UUID, status: str, reviewer: str, note: str) -> ReviewItem:
    """Human governance decision on a review item (audited; the item is kept, never deleted)."""
    if status not in RESOLUTION_STATUSES:
        raise ValueError(f"status must be one of {sorted(RESOLUTION_STATUSES)}")
    item = session.get(ReviewItem, review_id)
    if item is None:
        raise LookupError(f"no review item {review_id}")
    if item.status != "OPEN":
        raise ValueError(f"review item is already {item.status}")
    item.status, item.reviewer, item.resolved_at = status, reviewer, func.now()
    item.resolution = {"decision": status, "note": note}
    audit(session, f"review_{status.lower()}", "review_item", review_id, None, {"note": note}, actor=reviewer)
    return item
