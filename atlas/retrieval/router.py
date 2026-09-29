"""The `atlas ask` router: each question goes to the part of the system that can
answer it (evaluation rounds 1-3, docs/ask_evaluation.md).

  relationship  who / how many / how often / when / who at a company
                -> the graph answers directly from its facts. Exact: they cover
                   every email. Retrieval only sees a sample and miscounts.
  content       what was said
                -> brain retrieves and answers with citations; the graph's
                   profile of the people named rides along as context.
  mixed         both (call prep, "who is X and what do they talk about", deal
                history) -> both routes, returned as two labelled sections.
                They are not merged by a model: given both, a model recounts
                from the retrieved emails and overwrites the graph's numbers.

One small model call reads the question: the route, and the people and companies
it names as written ("Sarah", "Tom from Alloy"). The graph links those (ask.py);
when a name fits several people about equally, nothing is answered and the result
is a clarifying question with the candidates (route "clarify"): the caller asks
the user and asks again with the full name or email address. Assumptions made in
linking ("Sarah" -> Sarah Chen) are stated at the top of the answer.

Fallbacks (auto mode): a relationship or mixed question in which nobody could be
linked, and a relationship question the graph's facts do not answer, are answered
by content instead. For a relationship question the answer says so, and names the
people or companies the graph does not know; for a mixed one the search answer
stands alone (as for any content question), with `fallback` set in the result.
"""

from __future__ import annotations

import re
import time
from enum import Enum

from pydantic import BaseModel, Field

from atlas.config import Settings, get_settings
from atlas.db.session import session_scope
from atlas.retrieval.ask import ASKER, GraphContext, OrgMention, PersonMention, graph_context, system_prompt


class Route(str, Enum):
    RELATIONSHIP = "relationship"
    CONTENT = "content"
    MIXED = "mixed"


class Understanding(BaseModel):
    route: Route
    reason: str
    people: list[PersonMention]
    organizations: list[OrgMention]


class GraphAnswer(BaseModel):
    answer: str = Field(description="The answer, with the fact's marker such as [G3] after every claim "
                                    "taken from a fact.")
    answered: bool = Field(description="False when the facts do not answer the question.")


UNDERSTAND_PROMPT = """Read a question about a company's email. First classify it into one route.

relationship: answerable from email METADATA alone (who wrote to whom, and when): which people at
  a company someone dealt with, how many emails, how often, first or last contact, whether two people
  ever corresponded, most frequent contacts.
content: needs what the emails SAY: what was discussed, proposed, decided, asked, or what happened.
mixed: needs both, e.g. preparing for a call or meeting with someone, "who is X and what do they talk
  about", the history of a deal including who was involved.

Give the route and a short reason. Then list the people and the companies or organizations the
question names, exactly as written: full names, first names, nicknames, email addresses. For a
person tied to a company in the question ("Tom from Alloy", "Sarah at Acme"), give that company as
the person's organization and list the company too. For a company written as a well-known
abbreviation ("BofA", "GS"), give its full name as well; otherwise leave the full name empty. Leave out the person asking ("I", "me", "we",
"you", "our team") and their own company when referred to that way ("our company", "us"). List
nobody the question does not name."""

GRAPH_ONLY_PROMPT = """You answer questions about people and their email relationships using ONLY the
facts below. They are computed from the metadata of every email in the corpus (senders, recipients,
dates), with one person's several addresses already combined. They say nothing about what emails said.
Answer exactly from the facts: names, counts and dates as given. Each fact is followed by a marker
such as [G3] naming an email that shows it: put the marker after every claim you take from that fact.
A fact that there were none ("no emails together", "0 times") is an answer: say so, and it counts
as answered. If the facts do not answer the question, say so plainly instead of guessing, and set
answered to false.

{facts}
"""

GRAPH_PART_PROMPT = """From the facts below, answer ONLY the parts of the question about people and
relationships: who the people are, their organization, who they deal with most, how often, and first
or last contact. Skip anything about what was said; another part of the answer covers that. Be brief:
a few bullet points, names, counts and dates exactly as given, each followed by the fact's marker
(such as [G3]) naming an email that shows it. The facts are computed from the metadata of every email
in the corpus.

{facts}
"""


def _openai(settings: Settings):
    from openai import OpenAI

    return OpenAI(api_key=settings.openai_api_key)


def understand(question: str, settings: Settings) -> Understanding:
    """The route, and the people and companies the question names."""
    response = _openai(settings).beta.chat.completions.parse(
        model=settings.ask_model, temperature=0, response_format=Understanding,
        messages=[{"role": "system", "content": UNDERSTAND_PROMPT}, {"role": "user", "content": question}])
    return response.choices[0].message.parsed


def graph_answer(question: str, ctx: GraphContext, settings: Settings, part: bool = False) -> GraphAnswer:
    """Answer from graph facts alone, no retrieval. `part`: only the relationship part of a mixed question."""
    prompt = GRAPH_PART_PROMPT if part else GRAPH_ONLY_PROMPT
    response = _openai(settings).beta.chat.completions.parse(
        model=settings.ask_model, temperature=0, response_format=GraphAnswer,
        messages=[{"role": "system", "content": prompt.format(facts=ctx.facts or "(no entities recognized)")},
                  {"role": "user", "content": question}])
    return response.choices[0].message.parsed


def clarification(ambiguous: list[dict]) -> str:
    """A question back to the user, one block per ambiguous mention."""
    blocks = []
    for a in ambiguous:
        what = "company" if a["type"] == "Organization" else "person"
        count = "emails with you" if a["relative_to_asker"] else "emails"
        lines = [f'Which {what} do you mean by "{a["mention"]}"?']
        for i, c in enumerate(a["candidates"], 1):
            who = f'{c["name"]} <{c["email"]}>' if c.get("email") else c["name"]
            at = f', {c["organization"]}' if c.get("organization") else ""
            lines.append(f"  {i}. {who}{at} ({c['emails']} {count})")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\nAsk again with the full name or email address."


def brain_answer(question: str, ctx: GraphContext, settings: Settings, use_graph: bool = True) -> dict:
    """brain retrieves and answers with citations; the graph profile is context (use_graph)."""
    from brain import AccessScope, AnswerOptions

    from atlas.retrieval.brain_bridge import build_brain

    brain = build_brain(settings)
    options = AnswerOptions(force_search=True, system_prompt=system_prompt(ctx) if use_graph else None)
    # Citation numbers the model can write -> documents, recorded from every search the answer
    # loop runs (brain keeps the first mapping for a number, so this does too). brain's citation
    # events fire only on a document's first citation, so they cannot resolve a later number.
    mapping: dict[int, str] = {}
    search = brain.searcher.search

    def recording_search(*args, **kwargs):
        result = search(*args, **kwargs)
        for number, doc_id in result.citation_mapping.items():
            mapping.setdefault(number, doc_id)
        return result

    brain.searcher.search = recording_search
    answer, error, usage = [], None, None
    for event in brain.answer(question, access=AccessScope(bypass=True), options=options):
        kind = getattr(event, "type", "")
        if kind == "answer_delta":
            answer.append(event.text)
        elif kind == "answer_done":
            usage = event.usage
        elif kind == "answer_error":
            error = event.message
    text_ = "".join(answer)
    used = list(dict.fromkeys(int(n) for n in _SEARCH_MARKER.findall(text_)))
    return {"answer": text_, "error": error,
            "citations": [{"marker": str(n), "source": "search", **_document(mapping.get(n))} for n in used],
            "tokens": usage.model_dump() if usage else None}


_MARKER = re.compile(r"\[(G\d+)\]")
_SEARCH_MARKER = re.compile(r"\[\[(\d+)\]\]")


def _document(document_id: str | None) -> dict:
    """Title and date of an Atlas document, for showing a search citation."""
    from sqlalchemy import text

    if document_id is None:
        return {"document_id": None, "title": None, "date": None}
    with session_scope() as s:
        row = s.execute(text("SELECT title, source_created_at FROM kg.documents WHERE id = CAST(:i AS uuid)"),
                        {"i": document_id}).first()
    return {"document_id": document_id, "title": row.title if row else None,
            "date": row.source_created_at.isoformat() if row and row.source_created_at else None}


def graph_citations(answer: str, ctx: GraphContext) -> list[dict]:
    """The emails behind the [G#] markers an answer actually used, in order of first use."""
    used = list(dict.fromkeys(_MARKER.findall(answer)))
    return [{"marker": m, "source": "graph", "document_id": ctx.sources[m]["document_id"],
             "title": ctx.sources[m]["title"], "date": ctx.sources[m]["date"]} for m in used if m in ctx.sources]


def compose_mixed(graph_part: str, content: str) -> str:
    return f"**From the relationship graph** (email metadata, every email)\n\n{graph_part.strip()}\n\n" \
           f"**From the emails**\n\n{content.strip()}"


MODES = ("auto", "relationship", "content", "baseline")


def ask(question: str, mode: str = "auto", settings: Settings | None = None, asker: str | None = None) -> dict:
    """Answer one question. mode: auto (route it), relationship (graph only), content (brain +
    profile), baseline (brain alone). asker: email of the person asking, so "I" / "we" / "you"
    resolve. Route "clarify": a name fits several people; the answer is the question to ask the
    user, and `clarify` holds the candidates."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    settings = settings or get_settings()
    t0 = time.perf_counter()
    reading, ctx = None, GraphContext()
    if mode != "baseline":
        reading = understand(question, settings)
        with session_scope() as s:
            ctx = graph_context(s, question, asker_email=asker, people=reading.people,
                                organizations=reading.organizations)
    graph_ms = (time.perf_counter() - t0) * 1000

    route = {"relationship": Route.RELATIONSHIP, "content": Route.CONTENT, "baseline": Route.CONTENT}.get(mode)
    route = route or reading.route
    result = {"answer": "", "error": None, "citations": [], "tokens": None}
    fallback = None
    if ctx.ambiguous:
        route, result["answer"] = "clarify", clarification(ctx.ambiguous)
    else:
        named = [e for e in ctx.entities if e.matched != ASKER]
        if mode == "auto" and ((route is Route.MIXED and not named) or (route is Route.RELATIONSHIP and not ctx.entities)):
            fallback, route = "nobody the question names is in the relationship graph", Route.CONTENT
        if route is Route.RELATIONSHIP:
            graph = graph_answer(question, ctx, settings)
            if graph.answered or mode != "auto":
                result["answer"], result["citations"] = graph.answer, graph_citations(graph.answer, ctx)
            else:
                fallback, route = "the relationship graph's facts do not answer this", Route.CONTENT
        if route is Route.CONTENT:
            result = brain_answer(question, ctx, settings, use_graph=mode != "baseline")
        elif route is Route.MIXED:
            result = brain_answer(question, ctx, settings)
            result["graph_part"] = graph_answer(question, ctx, settings, part=True).answer
            result["answer"] = compose_mixed(result["graph_part"], result["error"] or result["answer"])
            result["citations"] = graph_citations(result["graph_part"], ctx) + result["citations"]
            result["error"] = None
        needed_graph = (reading.route if mode == "auto" else route) is Route.RELATIONSHIP
        notes = ctx.notes + (ctx.unknown + [f"Answered from search: {fallback}."] if needed_graph and fallback else [])
        if notes and result["answer"]:
            result["answer"] = "\n".join(f"_Note: {n}_" for n in notes) + "\n\n" + result["answer"]

    return {"question": question, "mode": mode, "route": getattr(route, "value", route),
            "route_reason": reading.reason if reading and mode == "auto" else None, "fallback": fallback,
            "entities": [{"name": e.name, "type": e.entity_type, "matched": e.matched} for e in ctx.entities],
            "notes": ctx.notes, "unknown": ctx.unknown, "clarify": ctx.ambiguous, "facts": ctx.facts, **result,
            "graph_ms": round(graph_ms), "seconds": round(time.perf_counter() - t0, 1)}
