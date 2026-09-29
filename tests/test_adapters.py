import json

import pytest

from atlas.ingestion.adapters import UnsupportedSource, normalize_file
from atlas.ingestion.adapters.email_json import split_quoted
from atlas.pipeline import segment
from tests.conftest import CORPUS


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


def test_split_quoted_handles_indented_and_forwarded_banners():
    from atlas.ingestion.adapters.email_json import split_quoted
    body = "Sounds good.\n\nPhillip\n\n -----Original Message-----\nFrom: \tTycholiz, B\nold text"
    assert split_quoted(body) == ("Sounds good.\n\nPhillip", "-----Original Message-----\nFrom: \tTycholiz, B\nold text")
    fwd = "FYI\n---------------------- Forwarded by John Arnold/HOU/ECT on 08/21/2000 12:26 PM ---------------------------\nbody"
    assert split_quoted(fwd)[0] == "FYI"


def test_split_quoted_handles_lotus_inline_replies():
    from atlas.ingestion.adapters.email_json import split_quoted
    body = "take me off your mailing list\n\n\n\nJobOpps@idrc.org on 09/05/2000 01:57:01 PM\nTo: jarnold@ei.enron.com\ncc:\nold"
    assert split_quoted(body)[0] == "take me off your mailing list"
    body2 = "Sounds good.\n\nJennifer Burns 10/12/2000 03:43 PM\n   To: John Arnold/HOU/ECT@ECT\nWell..."
    assert split_quoted(body2)[0] == "Sounds good."
    assert split_quoted("Meet at 10/12/2000 03:43 PM in room 5.\nThanks")[0].endswith("Thanks")  # no To: line


def test_placeholder_dates_become_unknown():
    from atlas.ingestion.adapters.email_json import EmailJsonAdapter
    doc = EmailJsonAdapter().normalize(None, {"message_id": "m1", "from": "a@acme.com", "body": "hi",
                                              "date": "1980-01-01T00:00:00+00:00"})
    assert doc.created_at is None and doc.metadata["rejected_created_at"] == "1980-01-01T00:00:00+00:00"
    ok = EmailJsonAdapter().normalize(None, {"message_id": "m2", "from": "a@acme.com", "body": "hi",
                                             "date": "2001-05-14T23:39:00+00:00"})
    assert ok.created_at.year == 2001 and "rejected_created_at" not in ok.metadata
    assert doc.checksum() == EmailJsonAdapter().normalize(None, {"message_id": "m1", "from": "a@acme.com",
                                                                 "body": "hi", "date": "1980-01-01T00:00:00+00:00"}).checksum()
