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

    def raise_item(self, review_type: str, dedupe_key: str, reason: str, payload: dict,
                   source_document_id: uuid.UUID | None = None, related_entities: list[uuid.UUID] | None = None,
                   confidence: float | None = None, example: dict | None = None) -> uuid.UUID:
        if review_type not in REVIEW_TYPES:
            raise ValueError(f"unknown review type {review_type}")
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
        examples = list(item.examples)
        if example and example not in examples and len(examples) < MAX_EXAMPLES:
            examples.append(example)
        if example is None or example not in item.examples:
            self.session.execute(update(ReviewItem).where(ReviewItem.id == item.id)
                                 .values(frequency=ReviewItem.frequency + 1, examples=examples,
                                         updated_at=func.now()))
        return item.id
