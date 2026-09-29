"""Extraction prompt. Bump PROMPT_VERSION whenever the text changes: it is part
of the extraction cache key and is recorded on every extraction run."""

from __future__ import annotations

PROMPT_VERSION = "kg-v1"

SYSTEM_PROMPT = """\
You extract a knowledge graph from a passage of text.

Entities
- Extract meaningful, specific entities: e.g. genes, proteins, mutations, drugs, \
diseases, clinical trials, organizations, people, regulatory bodies, pathways.
- Skip generic nouns ("patients", "results", "therapy") unless the text names a specific one.
- Each real-world entity appears ONCE. If the text uses several names for it \
(abbreviation, code name, brand name, former name), use the most complete standard \
name as `name` and put every other name used in the text in `aliases`.
- Keep distinct things distinct: a gene and one of its mutations are different entities \
(e.g. "KRAS" vs "KRAS G12C").
- `entity_type`: a short, general, reusable PascalCase type (e.g. Drug, Gene, Mutation, \
Disease, ClinicalTrial, Organization). Use the same type for the same kind of thing.
{entity_type_rule}
Relationships
- Only extract relationships the text explicitly states. Do not use background \
knowledge and do not infer speculative links.
- `relationship_type`: a short snake_case verb phrase in active voice, directed from \
source to target (e.g. targets, developed_by, evaluated_in, approved_for, subtype_of).
{relationship_type_rule}\
- `evidence`: copy the exact sentence or clause from the text that states the relationship. \
It must be a verbatim quote.
- Refer to entities by their `local_id`.
- `confidence`: how clearly the text states it (1.0 = stated directly).
"""

_DYNAMIC_ENTITY = ""  # Experiment A: no type constraints
_DYNAMIC_REL = ""


def build_system_prompt(
    allowed_entity_types: list[str] | None = None,
    allowed_relationship_types: list[str] | None = None,
) -> str:
    entity_rule = (
        f"- `entity_type` MUST be one of: {', '.join(allowed_entity_types)}. "
        "Skip entities that fit none of them.\n"
        if allowed_entity_types
        else _DYNAMIC_ENTITY
    )
    rel_rule = (
        f"- `relationship_type` MUST be one of: {', '.join(allowed_relationship_types)}. "
        "Skip relationships that fit none of them.\n"
        if allowed_relationship_types
        else _DYNAMIC_REL
    )
    return SYSTEM_PROMPT.format(entity_type_rule=entity_rule, relationship_type_rule=rel_rule)


def build_user_prompt(text: str, document_title: str | None = None) -> str:
    header = f"Document title: {document_title}\n\n" if document_title else ""
    return f"{header}Text:\n\"\"\"\n{text}\n\"\"\""
