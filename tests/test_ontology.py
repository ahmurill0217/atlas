import shutil
import uuid

import pytest
import yaml

from atlas.extraction.candidates import EvidenceRef
from atlas.ontology import MapStatus, OntologyError, load_ontology, map_entity_type, map_relation
from atlas.ontology.validator import validate_edge, validate_entity
from tests.conftest import ROOT

EV_STRUCT = EvidenceRef(document_id=uuid.uuid4(), source_field="from")
EV_TEXT = EvidenceRef(document_id=uuid.uuid4(), evidence_text="Sarah Chen from Acme")


def test_loads_with_stable_checksum(ontology):
    assert ontology.version == "1.1"
    assert {"Person", "Organization", "Document", "Project", "Event", "Location", "Team", "Meeting", "Product",
            "Contract", "Initiative", "Opportunity", "ActionItem"} == set(ontology.entity_types)
    assert {"Customer", "Vendor"} == set(ontology.roles)
    assert len(ontology.relations) == 22
    load_ontology.cache_clear()
    assert load_ontology(str(ROOT / "ontology")).checksum == ontology.checksum
    assert ontology.is_a("Meeting", "Event") and ontology.identity_fields("Meeting")[-1] == "event_external_id"


def _broken(tmp_path, filename, mutate):
    target = tmp_path / f"ontology_{uuid.uuid4().hex[:8]}"
    shutil.copytree(ROOT / "ontology", target)
    doc = yaml.safe_load((target / filename).read_text())
    mutate(doc)
    (target / filename).write_text(yaml.safe_dump(doc))
    load_ontology.cache_clear()
    with pytest.raises(OntologyError) as err:
        load_ontology(str(target))
    return "\n".join(err.value.problems)


def test_rejects_unknown_type_in_relation(tmp_path):
    msg = _broken(tmp_path, "core.yaml", lambda d: d["relations"]["WORKS_AT"].update(target=["Company"]))
    assert "unknown entity type 'Company'" in msg


def test_rejects_alias_collision_and_invalid_mapping(tmp_path):
    msg = _broken(tmp_path, "aliases.yaml", lambda d: d["relations"]["MANAGES"].append("owns"))
    assert "maps to both" in msg
    msg = _broken(tmp_path, "mappings.yaml", lambda d: d["relation_mappings"].append(
        {"suggested": "OWNS", "source": ["Document"], "target": ["Project"], "canonical": "OWNS_PROJECT"}))
    assert "produces an invalid OWNS_PROJECT edge" in msg


def test_rejects_version_mismatch(tmp_path):
    msg = _broken(tmp_path, "business.yaml", lambda d: d.update(version="9.9"))
    assert "disagree on version" in msg


def test_relation_mapping_never_invents(ontology):
    assert map_relation(ontology, "owns", "Person", "Project").canonical == "OWNS_PROJECT"          # alias
    assert map_relation(ontology, "OWNS", "Team", "Project").method == "mapping"                    # context mapping
    assert map_relation(ontology, "WORKS_AT", "Person", "Organization").method == "canonical"
    assert map_relation(ontology, "responsible for", "Person", "Project").canonical == "OWNS_PROJECT"
    assert map_relation(ontology, "OWNS", "Person", "Organization").status is MapStatus.TYPE_VIOLATION
    assert map_relation(ontology, "IS_HELPING_WITH", "Person", "Project").status is MapStatus.UNRESOLVED
    assert map_relation(ontology, "EXECUTIVE_SPONSOR_OF", "Person", "Project").status is MapStatus.UNRESOLVED
    assert map_relation(ontology, "WORKS_AT", "Document", "Meeting").status is MapStatus.TYPE_VIOLATION


def test_entity_type_mapping(ontology):
    m = map_entity_type(ontology, "customer company")
    assert (m.entity_type, m.role) == ("Organization", "Customer")
    assert map_entity_type(ontology, "Vendor").role == "Vendor"            # a role, not a new node type
    assert map_entity_type(ontology, "deal").entity_type == "Opportunity"
    assert map_entity_type(ontology, "task").entity_type == "ActionItem"
    assert map_entity_type(ontology, "Invoice").status is MapStatus.UNRESOLVED        # not in the ontology


def test_edge_validation(ontology):
    assert validate_edge(ontology, "WORKS_AT", "Person", "Organization", False, "EXPLICIT_TEXT", EV_TEXT) == []
    codes = {v.code for v in validate_edge(ontology, "WORKS_AT", "Document", "Meeting", False, "EXPLICIT_TEXT", EV_TEXT)}
    assert codes == {"SOURCE_TYPE_NOT_ALLOWED", "TARGET_TYPE_NOT_ALLOWED"}
    assert validate_edge(ontology, "WORKS_AT", "Contract", "Person", False, "EXPLICIT_TEXT", EV_TEXT)
    assert validate_edge(ontology, "HAS_PARTICIPANT", "Meeting", "Person", False, "STRUCTURED_SOURCE", EV_STRUCT) == []
    assert [v.code for v in validate_edge(ontology, "MANAGES", "Person", "Person", True, "EXPLICIT_TEXT", EV_TEXT)] == ["SELF_EDGE"]
    missing = validate_edge(ontology, "WORKS_AT", "Person", "Organization", False, "STRUCTURED_SOURCE", EV_TEXT)
    assert [v.code for v in missing] == ["MISSING_EVIDENCE"]
    assert [v.code for v in validate_edge(ontology, "HELPED_WITH", "Person", "Project", False, "EXPLICIT_TEXT", EV_TEXT)] == ["UNKNOWN_RELATION"]


def test_entity_validation(ontology):
    assert validate_entity(ontology, "Person", "Sarah", {"email": "s@a.com"}, {"job_title": "VP"}, []) == []
    codes = {v.code for v in validate_entity(ontology, "Person", None, {"domain": "a.com"}, {"color": "red"}, ["Customer"])}
    assert codes == {"UNKNOWN_IDENTIFIER_TYPE", "UNKNOWN_PROPERTY", "ROLE_NOT_APPLICABLE"}
    assert validate_entity(ontology, "Organization", "Acme", {}, {}, ["Customer"]) == []
    assert [v.code for v in validate_entity(ontology, "Renewal", "X", {}, {}, [])] == ["UNKNOWN_ENTITY_TYPE"]
