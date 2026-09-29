"""Chunk -> ExtractedKnowledgeGraph via the structured-output LLM.

Besides the LLM call, the extractor only cleans up its own output: it merges
duplicate entities inside a single extraction and drops relationships that
point at unknown local ids. Canonical identity is decided later by resolution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from brain_v0.extraction.models import (
    ExtractedEntity,
    ExtractedKnowledgeGraph,
    constrained_schema,
)
from brain_v0.extraction.prompts import PROMPT_VERSION, build_system_prompt, build_user_prompt
from brain_v0.llm.base import OutputTooLongError, StructuredLLM
from brain_v0.normalize import normalize_name


@dataclass
class ExtractionResult:
    graph: ExtractedKnowledgeGraph
    # Relationships removed during cleanup, with the reason (kept for the log).
    dropped: list[dict] = field(default_factory=list)


class KnowledgeGraphExtractor:
    def __init__(
        self,
        llm: StructuredLLM,
        allowed_entity_types: list[str] | None = None,
        allowed_relationship_types: list[str] | None = None,
    ):
        self.llm = llm
        self.allowed_entity_types = list(allowed_entity_types or [])
        self.allowed_relationship_types = list(allowed_relationship_types or [])
        self.schema = constrained_schema(self.allowed_entity_types, self.allowed_relationship_types)
        self.system_prompt = build_system_prompt(
            self.allowed_entity_types, self.allowed_relationship_types
        )

    @property
    def cache_key(self) -> str:
        """Identifies (model, prompt, ontology): same key => same extraction task."""
        payload = json.dumps(
            [self.llm.model_name, PROMPT_VERSION, self.system_prompt], sort_keys=True
        )
        return f"{PROMPT_VERSION}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"

    def extract(self, text: str, document_title: str | None = None, max_splits: int = 2) -> ExtractionResult:
        return clean_extraction(self._extract_raw(text, document_title, max_splits))

    def _extract_raw(self, text: str, document_title: str | None, max_splits: int) -> ExtractedKnowledgeGraph:
        try:
            raw = self.llm.generate(self.system_prompt, build_user_prompt(text, document_title), self.schema)
            return ExtractedKnowledgeGraph.model_validate(raw.model_dump())
        except OutputTooLongError:
            halves = split_in_half(text)
            if max_splits <= 0 or len(halves) < 2:
                raise
            # Too much to extract in one response: extract each half and merge.
            # Local ids are prefixed per half; clean_extraction then merges
            # entities that appear in both halves by name.
            parts = [self._extract_raw(h, document_title, max_splits - 1) for h in halves]
            return merge_graphs(parts)


def split_in_half(text: str) -> list[str]:
    """Split at the sentence boundary nearest the middle."""
    import re

    mid = len(text) // 2
    boundaries = [m.end() for m in re.finditer(r"[.!?]\s+", text)]
    if not boundaries:
        return [text]
    cut = min(boundaries, key=lambda b: abs(b - mid))
    parts = [text[:cut].strip(), text[cut:].strip()]
    return [p for p in parts if p]


def merge_graphs(parts: list[ExtractedKnowledgeGraph]) -> ExtractedKnowledgeGraph:
    entities, relationships = [], []
    for i, part in enumerate(parts):
        prefix = f"p{i}_"
        entities += [e.model_copy(update={"local_id": prefix + e.local_id}) for e in part.entities]
        relationships += [r.model_copy(update={"source_local_id": prefix + r.source_local_id,
                                               "target_local_id": prefix + r.target_local_id})
                          for r in part.relationships]
    return ExtractedKnowledgeGraph(entities=entities, relationships=relationships)


def clean_extraction(graph: ExtractedKnowledgeGraph) -> ExtractionResult:
    """Merge same-named entities within one extraction and drop dangling edges."""
    merged: dict[str, ExtractedEntity] = {}  # normalized name -> entity
    id_map: dict[str, str] = {}  # original local_id -> surviving local_id
    for entity in graph.entities:
        key = normalize_name(entity.name)
        if not key:
            continue
        if key in merged:
            keep = merged[key]
            aliases = [a for a in dict.fromkeys(keep.aliases + entity.aliases) if a]
            merged[key] = keep.model_copy(update={
                "aliases": aliases,
                "confidence": max(filter(None, [keep.confidence, entity.confidence]), default=None),
                "description": keep.description or entity.description,
            })
            id_map[entity.local_id] = keep.local_id
        else:
            merged[key] = entity
            id_map[entity.local_id] = entity.local_id

    relationships, dropped = [], []
    for rel in graph.relationships:
        src, tgt = id_map.get(rel.source_local_id), id_map.get(rel.target_local_id)
        if src is None or tgt is None:
            dropped.append({"relationship": rel.model_dump(), "reason": "unknown_local_id"})
            continue
        relationships.append(rel.model_copy(update={"source_local_id": src, "target_local_id": tgt}))

    return ExtractionResult(
        ExtractedKnowledgeGraph(entities=list(merged.values()), relationships=relationships), dropped
    )
