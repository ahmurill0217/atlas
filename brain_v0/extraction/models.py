"""Intermediate representation the LLM must return (cf. Cognee's
shared/data_models.KnowledgeGraph). This is a *proposal*: identities are local
to one chunk and nothing here is trusted until resolution/validation.

Fields have no defaults on purpose: OpenAI strict structured outputs require
every property to be present, and optional values are expressed as nullable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, create_model


class ExtractedEntity(BaseModel):
    local_id: str = Field(description="Short id unique within this extraction, e.g. 'e1'.")
    name: str = Field(description="Most complete, specific name used in the text.")
    entity_type: str = Field(description="General type in PascalCase, e.g. 'Drug', 'Organization'.")
    description: str | None = Field(description="One short sentence based only on the text.")
    aliases: list[str] = Field(
        description="Other names for this same entity that appear in the text "
        "(abbreviations, codes, brand names). Empty if none."
    )
    confidence: float | None = Field(description="0-1 confidence that this is a real entity.")


class ExtractedRelationship(BaseModel):
    source_local_id: str
    target_local_id: str
    relationship_type: str = Field(description="snake_case active-voice verb phrase, e.g. 'targets'.")
    description: str | None
    evidence: str | None = Field(
        description="Verbatim quote from the text that states this relationship."
    )
    confidence: float | None = Field(description="0-1 confidence the text states this relationship.")


class ExtractedKnowledgeGraph(BaseModel):
    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]


def constrained_schema(
    allowed_entity_types: list[str], allowed_relationship_types: list[str]
) -> type[ExtractedKnowledgeGraph]:
    """Experiment B: the same schema, but types become enums so structured
    output cannot produce anything outside the ontology."""
    if not allowed_entity_types and not allowed_relationship_types:
        return ExtractedKnowledgeGraph
    entity_model, relationship_model = ExtractedEntity, ExtractedRelationship
    if allowed_entity_types:
        entity_model = create_model(
            "ExtractedEntity",
            __base__=ExtractedEntity,
            entity_type=(Literal[tuple(allowed_entity_types)], ...),
        )
    if allowed_relationship_types:
        relationship_model = create_model(
            "ExtractedRelationship",
            __base__=ExtractedRelationship,
            relationship_type=(Literal[tuple(allowed_relationship_types)], ...),
        )
    return create_model(
        "ExtractedKnowledgeGraph",
        __base__=ExtractedKnowledgeGraph,
        entities=(list[entity_model], ...),
        relationships=(list[relationship_model], ...),
    )
