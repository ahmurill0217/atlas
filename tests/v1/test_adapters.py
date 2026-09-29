import json

import pytest

from atlas.ingestion.adapters import UnsupportedSource, normalize_file
from atlas.ingestion.adapters.email_json import split_quoted
from atlas.pipeline import segment
from tests.v1.conftest import CORPUS


def test_email_normalization():
    doc = normalize_file(CORPUS / "emails" / "2026-09-03_re_atlas.json")
    assert (doc.source_system, doc.source_type) == ("gmail", "email")
    assert doc.author.email == "sarah.chen@acme.com"          # lowercased from Sarah.Chen@ACME.com
    assert [(p.role, p.email) for p in doc.participants] == [
        ("sender", "sarah.chen@acme.com"), ("to", "mike.rodriguez@northwind.io")]
    assert [s.kind for s in doc.sections] == ["body", "quoted"]
    assert "wrote:" not in doc.sections[0].text and doc.sections[1].text.startswith("On Tue")
    assert doc.created_at.utcoffset().total_seconds() == -7 * 3600
    segment(doc)  # offsets match raw_text exactly
    for s in doc.sections:
        assert doc.raw_text[s.start_char:s.end_char] == s.text


def test_document_identity_and_checksum(tmp_path):
    src = CORPUS / "emails" / "2026-09-01_atlas_rollout.json"
    a, b = normalize_file(src), normalize_file(src)
    assert a.document_id == b.document_id and a.checksum() == b.checksum()
    payload = json.loads(src.read_text())
    payload["permissions"] = {"acl": ["someone"]}
    (tmp_path / "p.json").write_text(json.dumps(payload))
    assert normalize_file(tmp_path / "p.json").checksum() == a.checksum()   # permissions are not content
    payload["body"] += " Edited."
    (tmp_path / "e.json").write_text(json.dumps(payload))
    edited = normalize_file(tmp_path / "e.json")
    assert edited.document_id == a.document_id and edited.checksum() != a.checksum()


def test_meeting_normalization():
    doc = normalize_file(CORPUS / "meetings" / "2026-09-08_atlas_kickoff.json")
    assert doc.source_type == "meeting" and doc.title == "Atlas rollout kickoff"
    assert doc.created_at.isoformat() == "2026-09-08T17:01:10+00:00"           # recording start
    roles = {(p.role, p.email) for p in doc.participants}
    assert ("recorder", "mike.rodriguez@northwind.io") in roles
    assert ("speaker", "tom.becker@acme.com") in roles        # via matched_speaker_display_name "Tom (Acme)"
    assert ("invitee", "priya.shah@northwind.io") in roles
    assert not any(p.role == "speaker" and p.email == "priya.shah@northwind.io" for p in doc.participants)
    kinds = [s.kind for s in doc.sections]
    assert kinds[0] == "summary" and kinds.count("transcript_turn") == 4 and kinds.count("action_item") == 2
    segment(doc)


def test_text_and_unsupported(tmp_path):
    doc = normalize_file(CORPUS / "docs" / "atlas_project_notes.md")
    assert doc.title == "Atlas project notes" and doc.source_type == "text"
    (tmp_path / "x.json").write_text('{"hello": "world"}')
    with pytest.raises(UnsupportedSource):
        normalize_file(tmp_path / "x.json")


def test_split_quoted_variants():
    assert split_quoted("New text\n> old line") == ("New text", "> old line")
    assert split_quoted("Just text") == ("Just text", "")
