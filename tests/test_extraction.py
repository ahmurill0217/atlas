import json

import pytest
from pydantic import ValidationError

from brain.extraction import ExtractedKnowledgeGraph, KnowledgeGraphExtractor
from brain.extraction.extractor import clean_extraction
from brain.extraction.models import constrained_schema
from brain.normalize import (
    compact_name,
    normalize_entity_type,
    normalize_name,
    normalize_relationship_type,
    numeric_tokens,
)
from tests.conftest import FakeLLM, ent, graph, rel

RAW = json.dumps({
    "entities": [
        {"local_id": "e1", "name": "Sotorasib", "entity_type": "Drug", "description": None,
         "aliases": ["AMG 510"], "confidence": 0.95},
        {"local_id": "e2", "name": "KRAS G12C", "entity_type": "Mutation", "description": None,
         "aliases": [], "confidence": None},
    ],
    "relationships": [
        {"source_local_id": "e1", "target_local_id": "e2", "relationship_type": "targets",
         "description": None, "evidence": "Sotorasib ... targeting KRAS G12C", "confidence": 1.0},
    ],
})


def test_parses_structured_output():
    kg = ExtractedKnowledgeGraph.model_validate_json(RAW)
    assert kg.entities[0].aliases == ["AMG 510"]
    assert kg.relationships[0].relationship_type == "targets"


def test_rejects_malformed_output():
    with pytest.raises(ValidationError):
        ExtractedKnowledgeGraph.model_validate({"entities": [{"name": "x"}], "relationships": []})


def test_clean_merges_duplicate_entities_and_remaps_edges():
    kg = graph(
        [ent("e1", "Sotorasib", "Drug"), ent("e2", "sotorasib", "Drug", ["Lumakras"]), ent("e3", "KRAS", "Gene")],
        [rel("e2", "e3", "targets", "q"), rel("e1", "e9", "targets", "q")],
    )
    result = clean_extraction(kg)
    assert [e.name for e in result.graph.entities] == ["Sotorasib", "KRAS"]
    assert result.graph.entities[0].aliases == ["Lumakras"]
    assert result.graph.relationships[0].source_local_id == "e1"
    assert result.dropped[0]["reason"] == "unknown_local_id"


def test_constrained_schema_enforces_ontology():
    schema = constrained_schema(["Drug"], ["targets"])
    ok = {"entities": [ent("e1", "X", "Drug")], "relationships": []}
    schema.model_validate(ok)
    with pytest.raises(ValidationError):
        schema.model_validate({"entities": [ent("e1", "X", "Gene")], "relationships": []})
    assert constrained_schema([], []) is ExtractedKnowledgeGraph


def test_extractor_with_mock_llm_and_cache_key():
    llm = FakeLLM({"Sotorasib": json.loads(RAW)})
    extractor = KnowledgeGraphExtractor(llm)
    result = extractor.extract("Sotorasib targets KRAS G12C.")
    assert len(result.graph.entities) == 2 and llm.calls == 1
    assert extractor.cache_key != KnowledgeGraphExtractor(llm, ["Drug"]).cache_key


def test_normalizers():
    assert normalize_name("KRAS-G12C") == normalize_name("kras g12c") == "kras g12c"
    assert normalize_name("Amgen's") == "amgen"
    assert compact_name("AMG 510") == compact_name("AMG510")
    assert numeric_tokens("CodeBreaK 100") != numeric_tokens("CodeBreaK 200")
    assert normalize_entity_type("clinical trial") == normalize_entity_type("ClinicalTrial") == "ClinicalTrial"
    assert normalize_relationship_type("Developed By") == normalize_relationship_type("developedBy") == "developed_by"


def test_output_too_long_splits_chunk_and_merges():
    from brain.llm.base import OutputTooLongError

    class TooLongForFullChunk:
        model_name = "fake"

        def __init__(self):
            self.calls = []

        def generate(self, system, user, schema):
            self.calls.append(user)
            if "Sotorasib targets KRAS G12C." in user and "Amgen developed sotorasib." in user:
                raise OutputTooLongError("length limit")
            if "targets" in user:
                return schema.model_validate({"entities": [ent("e1", "Sotorasib", "Drug"), ent("e2", "KRAS G12C", "Mutation")],
                                              "relationships": [rel("e1", "e2", "targets", "Sotorasib targets KRAS G12C.")]})
            return schema.model_validate({"entities": [ent("e1", "Amgen", "Organization"), ent("e2", "sotorasib", "Drug")],
                                          "relationships": [rel("e2", "e1", "developed_by", "Amgen developed sotorasib.")]})

    llm = TooLongForFullChunk()
    result = KnowledgeGraphExtractor(llm).extract("Sotorasib targets KRAS G12C. Amgen developed sotorasib.")
    assert len(llm.calls) == 3
    assert sorted(e.name for e in result.graph.entities) == ["Amgen", "KRAS G12C", "Sotorasib"]  # merged across halves
    ids = {e.local_id for e in result.graph.entities}
    assert all(r.source_local_id in ids and r.target_local_id in ids for r in result.graph.relationships)
    assert len(result.graph.relationships) == 2
