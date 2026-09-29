"""The tool layer (atlas.retrieval.tools), the verifier and the answer loop; no model calls."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.ontology import load_ontology
from atlas.pipeline import KnowledgeIngestionPipeline
from atlas.retrieval import agent
from atlas.retrieval.agent import _fit
from atlas.retrieval.tools import AtlasTools, Entry, Ledger, _day
from atlas.retrieval.verify import render, verify
from tests.conftest import ROOT


@pytest.fixture
def office(kg, atlas_settings, tmp_path):
    """Angel emails Joe twice, Joe replies once, a newsletter writes to Angel; all three
    plus Claudia (invited, silent) are on one meeting."""
    mails = [("angel.murillo@alloytx.com", ["joe.finnell@alloytx.com"], "Benchling write-back", "2026-09-01"),
             ("angel.murillo@alloytx.com", ["joe.finnell@alloytx.com"], "HPLC templates", "2026-09-10"),
             ("joe.finnell@alloytx.com", ["angel.murillo@alloytx.com"], "Re: HPLC templates", "2026-09-11"),
             ("news@vendor.io", ["angel.murillo@alloytx.com"], "Weekly digest", "2026-09-12")]
    for i, (sender, to, subject, day) in enumerate(mails):
        (tmp_path / f"m{i}.json").write_text(json.dumps({"source_system": "test", "message_id": f"t{i}",
                                                         "from": sender, "to": to, "subject": subject,
                                                         "date": f"{day}T10:00:00Z", "body": "x"}))
    invitees = [("Angel Murillo", "angel.murillo@alloytx.com"), ("Joe Finnell", "joe.finnell@alloytx.com"),
                ("Claudia Vesel", "claudia.vesel@alloytx.com")]
    (tmp_path / "meeting.json").write_text(json.dumps({
        "source_system": "fathom", "title": "Darwin Standup", "recording_id": 7, "created_at": "2026-09-23T15:00:00Z",
        "recording_start_time": "2026-09-23T15:00:00Z",
        "calendar_invitees": [{"name": n, "email": e, "email_domain": "alloytx.com", "is_external": False,
                               "matched_speaker_display_name": n if n != "Claudia Vesel" else None} for n, e in invitees],
        "transcript": [{"speaker": {"display_name": "Joe Finnell", "matched_calendar_invitee_email": "joe.finnell@alloytx.com"},
                        "text": "Writing to Benchling needs an audit trail.", "timestamp": "0:10"},
                       {"speaker": {"display_name": "Angel Murillo", "matched_calendar_invitee_email": "angel.murillo@alloytx.com"},
                        "text": "We keep who approved it in Darwin.", "timestamp": "0:20"}]}))
    settings = atlas_settings.model_copy(update={"internal_domains": ["alloytx.com"]})
    KnowledgeIngestionPipeline(kg, settings, load_ontology(str(ROOT / "ontology"))).ingest(tmp_path)
    return kg, settings


def _id(tools, name, type_=None):
    return next(c["id"] for c in tools.find(name, type_)["candidates"] if c["name"] == name)


def test_emails_and_meetings_are_one_kind_of_interaction(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings, asker_email="angel.murillo@alloytx.com")
        joe = tools.find("Joe", None)
        pair = tools.interactions(tools.asker["id"], _id(tools, "Joe Finnell"), "any", None, None, 10)
        since = tools.interactions(tools.asker["id"], _id(tools, "Joe Finnell"), "email", "2026-09-05", None, 10)
    assert joe["candidates"][0]["name"] == "Joe Finnell" and "note" not in joe
    summary = pair["summary"]
    assert summary["total"] == 4 and summary["meetings together"] == 1 and summary["last_date"] == "2026-09-23"
    assert summary["emails Angel Murillo wrote to Joe Finnell"] == 2
    assert summary["emails Joe Finnell wrote to Angel Murillo"] == 1
    assert pair["last meeting together"]["title"] == "Darwin Standup"
    assert pair["last email Joe Finnell to Angel Murillo"]["date"] == "2026-09-11"
    assert since["summary"]["total"] == 2                                   # a time window, emails only


def test_participants_include_invitees_who_did_not_speak(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings)
        meeting = tools.find("darwin standup", "Meeting")["candidates"][0]
        people = {p["name"]: p["roles"] for p in tools.participants(meeting["id"])["people"]}
    assert meeting["date"].startswith("2026-09-23")
    assert people["Claudia Vesel"] == ["invitee"] and sorted(people["Joe Finnell"]) == ["invitee", "speaker"]


def test_contacts_leave_out_broadcast_senders(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings)
        names = [c["name"] for c in tools.contacts(_id(tools, "Angel Murillo"), "any", None, None, "all", 10)["contacts"]]
    assert names[0] == "Joe Finnell" and "Claudia Vesel" in names and not any("news" in n for n in names)


def test_read_shows_who_said_what(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings)
        doc = tools.participants(tools.find("Darwin Standup", "Meeting")["candidates"][0]["id"])["document_id"]
        page = tools.read(doc, 0)
    assert "Joe Finnell: Writing to Benchling needs an audit trail." in page["text"] and page["ref"] == "S1"


def _ledger():
    ledger = Ledger()
    ledger.add(Entry("text", {}, "Joe Finnell: Writing to Benchling needs an audit trail.", "doc-1", "Standup", "2026-09-23"))
    ledger.add(Entry("graph", {}, json.dumps({"id": "0b7c1f7e-54a1-4c3e-9d1a-2f4e8c9a1b2c", "title": "Standup",
                                              "date": "2026-09-23", "total": 4}), "doc-1", "Standup", "2026-09-23"))
    return ledger


@pytest.mark.parametrize("part, problem", [
    ({"text": "Joe said writing to Benchling needs an audit trail.",
      "citations": [{"ref": "S1", "quote": "Writing to Benchling needs an audit trail"}]}, None),
    ({"text": "They met on September 23, 2026, four times in all (4).", "citations": [{"ref": "G1", "quote": None}]}, None),
    ({"text": "The audit is on September 30, 2026.",
      "citations": [{"ref": "S1", "quote": "needs an audit trail"}]}, "numbers not in the cited evidence: [30, 2026]"),
    ({"text": "Joe wants a signed audit report.",
      "citations": [{"ref": "S1", "quote": "needs a signed audit report"}]}, "the quote is not in that passage"),
    ({"text": "Joe raised it.", "citations": [{"ref": "S9", "quote": "anything at all here"}]}, "not returned by any tool"),
    ({"text": "The meeting had 6 people.", "citations": []}, "needs citations"),
    ({"text": "The sources do not say.", "citations": []}, None),
    ({"text": "Their id is 2, and 8 people.", "citations": [{"ref": "G1", "quote": None}]}, "[2, 8]"),  # ids hold no facts
])
def test_verifier(part, problem):
    report = verify([part], _ledger())
    if problem is None:
        assert report["accepted"], report
    else:
        assert problem in " ".join(report["failed"][0]["problems"])


def test_render_drops_what_failed_and_lists_citations():
    ledger = _ledger()
    parts = [{"text": "Joe said Benchling writes need an audit trail.",
              "citations": [{"ref": "S1", "quote": "needs an audit trail"}]},
             {"text": "The audit is on September 30.", "citations": [{"ref": "S1", "quote": "needs an audit trail"}]}]
    answer, citations, dropped = render(parts, verify(parts, ledger), ledger)
    assert answer == "Joe said Benchling writes need an audit trail [S1]."
    assert citations == [{"marker": "S1", "source": "search", "document_id": "doc-1", "title": "Standup",
                          "date": "2026-09-23", "quote": "needs an audit trail"}]
    assert dropped[0]["text"] == "The audit is on September 30."


class _Scripted:
    """A fake chat client that plays back tool calls, one assistant turn per create()."""

    def __init__(self, turns):
        self.turns, self.seen = list(turns), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.seen.append(kwargs["messages"][-1])
        name, args = self.turns.pop(0)
        call = SimpleNamespace(id=f"c{len(self.turns)}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        message = SimpleNamespace(tool_calls=[call], model_dump=lambda **_: {"role": "assistant", "tool_calls": [
            {"id": call.id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def test_the_loop_sends_failures_back_and_keeps_what_verifies(office, monkeypatch):
    kg, settings = office
    monkeypatch.setattr(agent, "session_scope", lambda: Session(kg))
    with Session(kg) as s:
        doc = AtlasTools(s, settings).participants(
            AtlasTools(s, settings).find("Darwin Standup", "Meeting")["candidates"][0]["id"])["document_id"]
    bad = [{"text": "The Benchling audit is on September 30.", "citations": [{"ref": "S1", "quote": "needs an audit trail"}]}]
    good = [{"text": "Joe said writing to Benchling needs an audit trail.",
             "citations": [{"ref": "S1", "quote": "Writing to Benchling needs an audit trail."}]}]
    client = _Scripted([("read", {"document_id": doc, "offset": 0}),
                        ("submit_answer", {"clarify": None, "parts": bad}),
                        ("submit_answer", {"clarify": None, "parts": good})])
    out = agent.answer("What did Joe say about Benchling?", settings=settings, client=client)
    rejected = json.loads(client.seen[-1]["content"])
    assert rejected["accepted"] is False and "30" in rejected["failed"][0]["problems"][0]
    assert out["answer"] == "Joe said writing to Benchling needs an audit trail [S1]."
    assert out["verification"] == {"parts": 1, "failed": 0, "submissions": 2}


def test_a_clarifying_question_ends_the_loop(office, monkeypatch):
    kg, settings = office
    monkeypatch.setattr(agent, "session_scope", lambda: Session(kg))
    client = _Scripted([("submit_answer", {"clarify": "Which Sarah do you mean?", "parts": []})])
    out = agent.answer("Prep me for Sarah", settings=settings, client=client)
    assert out["route"] == "clarify" and out["answer"] == "Which Sarah do you mean?"


def test_interactions_with_a_company_break_down_by_person(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings, asker_email="angel.murillo@alloytx.com")
        company = next(c["id"] for c in tools.find("alloytx", "Organization")["candidates"])
        people = {p["name"]: p for p in tools.interactions(tools.asker["id"], company, "any", None, None, 5)["by person"]}
    assert people["Joe Finnell"]["emails_and_meetings"] == 4 and people["Claudia Vesel"]["emails_and_meetings"] == 1
    assert people["Joe Finnell"]["emails they wrote to Angel Murillo"] == 1
    assert people["Joe Finnell"]["emails Angel Murillo wrote to them"] == 2
    assert (people["Joe Finnell"]["first_date"], people["Joe Finnell"]["last_date"]) == ("2026-09-01", "2026-09-23")


def test_the_loop_retries_a_transient_api_error(office, monkeypatch):
    import httpx
    import openai

    kg, settings = office
    monkeypatch.setattr(agent, "session_scope", lambda: Session(kg))
    monkeypatch.setattr(agent, "RETRY_SECONDS", 0)
    client = _Scripted([("submit_answer", {"clarify": None, "parts": [{"text": "The sources do not say.", "citations": []}]})])
    play, failures = client.chat.completions.create, iter([openai.APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com"))])

    def flaky(**kwargs):
        for exc in failures:
            raise exc
        return play(**kwargs)

    client.chat.completions.create = flaky
    out = agent.answer("Anything?", settings=settings, client=client)
    assert out["answer"] == "The sources do not say." and out["error"] is None


@pytest.mark.parametrize("value, day", [("2026-09-05", "2026-09-05"), ("2026-09", "2026-09-01"),
                                        ("2026-09-05T13:00:00", "2026-09-05"), (" 2026-09-05 ", "2026-09-05"),
                                        (None, None), ("", None)])
def test_dates_are_read_as_iso_days(value, day):
    assert _day("since", value) == day


@pytest.mark.parametrize("value", ["May 2001", "2001-5-1", "09/05/2026", "last week"])
def test_other_date_forms_are_refused(value):
    with pytest.raises(ValueError, match="since must be an ISO date"):
        _day("since", value)


def test_a_bad_date_comes_back_as_an_error_not_wrong_rows(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings, asker_email="angel.murillo@alloytx.com")
        joe = _id(tools, "Joe Finnell")
        args = {"entity": tools.asker["id"], "other": joe, "kind": "email", "until": None, "limit": 10}
        bad = tools.call("interactions", {**args, "since": "Sept 5 2026"})
        month = tools.call("interactions", {**args, "since": "2026-09"})
        stamp = tools.call("interactions", {**args, "since": "2026-09-05T00:00:00"})
        contacts = tools.call("contacts", {"entity": joe, "kind": "any", "since": "2026-9-1", "until": None,
                                           "scope": "all", "limit": 5})
    assert bad == {"error": "since must be an ISO date like 2001-05-01, not 'Sept 5 2026'"}
    assert month["summary"]["total"] == 3 and stamp["summary"]["total"] == 2
    assert "since must be an ISO date" in contacts["error"]


def test_a_failed_query_is_an_error_and_the_session_recovers(office):
    kg, settings = office
    with Session(kg) as s:
        tools = AtlasTools(s, settings)
        tools.find = lambda **_: s.execute(text("SELECT 1 / 0"))            # a real database error
        failed = tools.call("find", {"name": "Joe", "type": None})
        del tools.find
        after = tools.call("find", {"name": "Joe Finnell", "type": "Person"})
    assert failed["error"].startswith("the query failed: division by zero")
    assert after["candidates"][0]["name"] == "Joe Finnell"


def test_large_results_are_trimmed_by_whole_items():
    passages = [{"ref": f"S{i}", "text": "x" * 1000} for i in range(50)]
    text_ = _fit({"query": "q", "passages": passages}, limit=10_000)
    out = json.loads(text_)
    assert len(text_) <= 10_000 and out["truncated"] is True and out["query"] == "q"
    assert [p["ref"] for p in out["passages"]] == [f"S{i}" for i in range(len(out["passages"]))]   # best kept
    assert out["note"].startswith(f"{50 - len(out['passages'])} items left out")
    assert _fit({"a": 1}, limit=100) == '{"a": 1}'
    huge = json.loads(_fit({"ref": "S1", "title": "t", "text": "x" * 500}, limit=100))
    assert huge == {"ref": "S1", "title": "t", "truncated": True,
                    "note": "the result was too large to show; narrow the query"}
