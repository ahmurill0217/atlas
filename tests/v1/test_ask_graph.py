"""Graph side of `atlas ask`: entity linking and the metadata profile."""

import json

import pytest
from sqlalchemy.orm import Session

from atlas.ontology import load_ontology
from atlas.pipeline import KnowledgeIngestionPipeline
from atlas.retrieval.ask import graph_context
from tests.v1.conftest import ROOT


@pytest.fixture
def mailbox(kg, atlas_settings, tmp_path):
    msgs = [("phillip.allen@enron.com", ["keith.holst@enron.com"], "Bishops Corner", "2001-03-14T10:00:00Z"),
            ("keith.holst@enron.com", ["phillip.allen@enron.com"], "Re: Bishops Corner", "2001-03-15T10:00:00Z"),
            ("deals@pizzahut.com", ["phillip.allen@enron.com"], "Ring in the New Year", "2001-12-31T10:00:00Z"),
            ("phillip.allen@enron.com", ["mike.grigsby@enron.com"], "West desk", "1980-01-01T00:00:00Z")]
    for i, (sender, to, subject, date) in enumerate(msgs):
        (tmp_path / f"{i}.json").write_text(json.dumps({"source_system": "test", "message_id": f"m{i}",
                                                        "from": sender, "to": to, "subject": subject, "date": date,
                                                        "body": "text"}))
    settings = atlas_settings.model_copy(update={"internal_domains": ["enron.com"]})
    KnowledgeIngestionPipeline(kg, settings, load_ontology(str(ROOT / "ontology" / "v1_3"))).ingest(tmp_path)
    return kg


def test_profile_leaves_out_broadcast_mail_and_placeholder_dates(mailbox):
    with Session(mailbox) as s:
        ctx = graph_context(s, "What do I know about Phillip Allen?")
    assert [e.name for e in ctx.entities] == ["Phillip Allen"]
    assert "Keith Holst (2, last 2001-03-15 [G1])" in ctx.facts and "Mike Grigsby (1, last ?" in ctx.facts
    assert "Pizza" not in ctx.facts and "Phillip Allen (1" not in ctx.facts  # no broadcasts, not himself
    assert "active 2001-03-14 to 2001-03-15" in ctx.facts                  # 1980 placeholder ignored


def test_two_people_get_pair_facts(mailbox):
    with Session(mailbox) as s:
        ctx = graph_context(s, "What did Keith Holst and Phillip Allen discuss?")
    assert {e.name for e in ctx.entities} == {"Keith Holst", "Phillip Allen"}
    assert "Keith Holst and Phillip Allen: 2 emails/meetings with both on them" in ctx.facts


def test_unknown_names_link_nothing(mailbox):
    with Session(mailbox) as s:
        ctx = graph_context(s, "What happened with the Bishop's Corner partnership?")
    assert ctx.entities == [] and ctx.facts == ""


def test_a_single_word_never_links_a_person(mailbox):
    with Session(mailbox) as s:
        assert graph_context(s, "Prep me for a call with Keith.").entities == []
        assert graph_context(s, "What did Holst say about Bishops Corner?").entities == []
        assert [e.name for e in graph_context(s, "Prep me for keith.holst@enron.com").entities] == ["Keith Holst"]


def test_relationship_facts(kg, atlas_settings, tmp_path):
    msgs = [("john.arnold@enron.com", ["steve.lafontaine@bankofamerica.com"], "mkts", "2001-05-04T10:00:00Z"),
            ("steve.lafontaine@bankofamerica.com", ["john.arnold@enron.com"], "re: mkts", "2001-12-11T10:00:00Z"),
            ("mike.grigsby@enron.com", ["john.arnold@enron.com"], "desk", "2001-06-01T10:00:00Z")]
    for i, (sender, to, subject, date) in enumerate(msgs):
        (tmp_path / f"{i}.json").write_text(json.dumps({"source_system": "test", "message_id": f"r{i}", "from": sender,
                                                        "to": to, "subject": subject, "date": date, "body": "x"}))
    settings = atlas_settings.model_copy(update={"internal_domains": ["enron.com"]})
    KnowledgeIngestionPipeline(kg, settings, load_ontology(str(ROOT / "ontology" / "v1_3"))).ingest(tmp_path)
    with Session(kg) as s:
        bofa = graph_context(s, "Who at Bank of America has John Arnold emailed with?")
        pair = graph_context(s, "Has Mike Grigsby ever emailed Steve Lafontaine?")
        arnold = graph_context(s, "When did John Arnold and Steve Lafontaine last talk?")
    assert {e.name for e in bofa.entities} == {"John Arnold", "bankofamerica.com"}
    assert ("People at bankofamerica.com who have dealt with John Arnold (1 addresses; one person may use several): "
            "Steve Lafontaine <steve.lafontaine@bankofamerica.com> (2 emails, 2001-05-04 to 2001-12-11 [G1])") in bofa.facts
    assert "Mike Grigsby and Steve Lafontaine: no emails or meetings together in the corpus." in pair.facts
    assert ("John Arnold wrote to Steve Lafontaine 1 times, last 2001-05-04 [G2]; Steve Lafontaine wrote to "
            "John Arnold 1 times, last 2001-12-11 [G1]. First email with both on it 2001-05-04 [G2], "
            "last 2001-12-11 [G1].") in arnold.facts
    assert "outside our organization: Steve Lafontaine (2, last 2001-12-11 [G1])" in arnold.facts
    # every marker resolves to a real document: the email that shows the fact
    assert {src["title"] for src in arnold.sources.values()} == {"mkts", "re: mkts"}
    assert arnold.sources["G2"]["title"] == "mkts" and arnold.sources["G2"]["date"].startswith("2001-05-04")


def test_copied_on_the_same_email_is_not_writing_to_each_other(kg, atlas_settings, tmp_path):
    msgs = [("phillip.allen@enron.com", ["keith.holst@enron.com"], "W basis quotes", "2000-02-09T10:00:00Z"),
            ("ina.rangel@enron.com", ["phillip.allen@enron.com", "keith.holst@enron.com"], "ICE Presentation",
             "2001-05-14T10:00:00Z")]
    for i, (sender, to, subject, date) in enumerate(msgs):
        (tmp_path / f"{i}.json").write_text(json.dumps({"source_system": "test", "message_id": f"c{i}", "from": sender,
                                                        "to": to, "subject": subject, "date": date, "body": "x"}))
    settings = atlas_settings.model_copy(update={"internal_domains": ["enron.com"]})
    KnowledgeIngestionPipeline(kg, settings, load_ontology(str(ROOT / "ontology" / "v1_3"))).ingest(tmp_path)
    with Session(kg) as s:
        ctx = graph_context(s, "When did Phillip Allen last email Keith Holst?")
        mine = graph_context(s, "How often do we talk to Keith Holst?", asker_email="phillip.allen@enron.com")
    line = next(f for f in ctx.facts.split("\n") if f.startswith("- Phillip Allen and Keith Holst"))
    cited = {m: src["title"] for m, src in ctx.sources.items()}
    last_written = line.split("Phillip Allen wrote to Keith Holst 1 times, last ")[1].split(";")[0]
    assert last_written.startswith("2000-02-09") and cited[last_written.split("[")[1].rstrip("]")] == "W basis quotes"
    assert "Keith Holst wrote to Phillip Allen 0 times, last never" in line
    assert "last 2001-05-14" in line.split("First email with both on it")[1]      # the list email, labelled as such
    assert mine.facts.startswith('- The person asking is Phillip Allen (phillip.allen@enron.com)')
    assert "Phillip Allen and Keith Holst" in mine.facts
