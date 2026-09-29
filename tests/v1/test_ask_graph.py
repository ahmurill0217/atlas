"""Graph side of `atlas ask`: entity linking, scope, and the metadata profile."""

import json
import uuid

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


def test_scope_and_profile_leave_out_broadcast_mail_and_placeholder_dates(mailbox):
    with Session(mailbox) as s:
        ctx = graph_context(s, "What do I know about Phillip Allen?")
    assert [e.name for e in ctx.entities] == ["Phillip Allen"]
    assert len(ctx.document_ids) == 3                       # the Pizza Hut broadcast is out of scope
    assert "Keith Holst (2)" in ctx.facts and "Mike Grigsby (1)" in ctx.facts
    assert "Pizza" not in ctx.facts and "Phillip Allen (1" not in ctx.facts  # no broadcasts, not himself
    assert "active 2001-03-14 to 2001-03-15" in ctx.facts                  # 1980 placeholder ignored


def test_two_people_scope_to_shared_documents(mailbox):
    with Session(mailbox) as s:
        ctx = graph_context(s, "What did Keith Holst and Phillip Allen discuss?")
    assert {e.name for e in ctx.entities} == {"Keith Holst", "Phillip Allen"}
    assert ctx.scope == "shared" and len(ctx.document_ids) == 2


def test_unknown_names_leave_the_search_unscoped(mailbox):
    with Session(mailbox) as s:
        ctx = graph_context(s, "What happened with the Bishop's Corner partnership?")
    assert ctx.entities == [] and ctx.document_ids == [] and ctx.scope == "unscoped"
