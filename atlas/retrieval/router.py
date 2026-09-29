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

The route is chosen by a small classification call. With no person or company
recognized, the graph has nothing to add and the question goes to content.
"""

from __future__ import annotations

import re
import time
from enum import Enum

from pydantic import BaseModel

from atlas.config import Settings, get_settings
from atlas.db.session import session_scope
from atlas.retrieval.ask import GraphContext, graph_context, system_prompt


class Route(str, Enum):
    RELATIONSHIP = "relationship"
    CONTENT = "content"
    MIXED = "mixed"


class RouteDecision(BaseModel):
    route: Route
    reason: str


CLASSIFY_PROMPT = """Classify a question about a company's email into one route.

relationship: answerable from email METADATA alone (who wrote to whom, and when): which people at
  a company someone dealt with, how many emails, how often, first or last contact, whether two people
  ever corresponded, most frequent contacts.
content: needs what the emails SAY: what was discussed, proposed, decided, asked, or what happened.
mixed: needs both, e.g. preparing for a call or meeting with someone, "who is X and what do they talk
  about", the history of a deal including who was involved.

Answer with the route and a short reason."""

GRAPH_ONLY_PROMPT = """You answer questions about people and their email relationships using ONLY the
facts below. They are computed from the metadata of every email in the corpus (senders, recipients,
dates), with one person's several addresses already combined. They say nothing about what emails said.
Answer exactly from the facts: names, counts and dates as given. Each fact is followed by a marker
such as [G3] naming an email that shows it: put the marker after every claim you take from that fact.
If the facts do not answer the question, say so plainly instead of guessing.

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


def classify(question: str, ctx: GraphContext, settings: Settings) -> RouteDecision:
    if not ctx.entities:
        return RouteDecision(route=Route.CONTENT, reason="no person or company recognized; the graph has nothing to add")
    response = _openai(settings).beta.chat.completions.parse(
        model=settings.ask_model, temperature=0, response_format=RouteDecision,
        messages=[{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": question}])
    return response.choices[0].message.parsed


def graph_answer(question: str, ctx: GraphContext, settings: Settings, part: bool = False) -> str:
    """Answer from graph facts alone, no retrieval. `part`: only the relationship part of a mixed question."""
    prompt = GRAPH_PART_PROMPT if part else GRAPH_ONLY_PROMPT
    response = _openai(settings).chat.completions.create(
        model=settings.ask_model, temperature=0,
        messages=[{"role": "system", "content": prompt.format(facts=ctx.facts or "(no entities recognized)")},
                  {"role": "user", "content": question}])
    return response.choices[0].message.content or ""


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
    profile), baseline (brain alone). asker: email of the
    person asking, so "I" / "we" / "you" resolve."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    settings = settings or get_settings()
    t0 = time.perf_counter()
    with session_scope() as s:
        ctx = graph_context(s, question, asker_email=asker) if mode != "baseline" else GraphContext()
    graph_ms = (time.perf_counter() - t0) * 1000

    decision = None
    route = {"relationship": Route.RELATIONSHIP, "content": Route.CONTENT, "baseline": Route.CONTENT}.get(mode)
    if mode == "auto":
        decision = classify(question, ctx, settings)
        route = decision.route

    result = {"answer": "", "error": None, "citations": [], "tokens": None}
    if route is Route.RELATIONSHIP:
        result["answer"] = graph_answer(question, ctx, settings)
        result["citations"] = graph_citations(result["answer"], ctx)
    elif route is Route.CONTENT:
        result = brain_answer(question, ctx, settings, use_graph=mode != "baseline")
    else:
        result = brain_answer(question, ctx, settings)
        result["graph_part"] = graph_answer(question, ctx, settings, part=True)
        result["answer"] = compose_mixed(result["graph_part"], result["error"] or result["answer"])
        result["citations"] = graph_citations(result["graph_part"], ctx) + result["citations"]
        result["error"] = None

    return {"question": question, "mode": mode, "route": route.value,
            "route_reason": decision.reason if decision else None,
            "entities": [{"name": e.name, "type": e.entity_type, "matched": e.matched} for e in ctx.entities],
            "facts": ctx.facts, **result,
            "graph_ms": round(graph_ms), "seconds": round(time.perf_counter() - t0, 1)}
