"""A tool-using answer loop over Atlas and brain, for tests and evaluation.

A platform with its own agent uses the same pieces: `AtlasTools` (the tools and their
schemas), `ANSWER_SCHEMA` (how an answer is submitted) and `verify` / `render` (the check
every answer passes through). This loop is the smallest thing that runs them: the model
calls tools until it submits; a submission that fails verification goes back with the
problems, up to MAX_SUBMISSIONS times; what still fails is dropped from the answer and
reported, never shown as fact.
"""

from __future__ import annotations

import json
import time
from datetime import date

from atlas.config import Settings, get_settings
from atlas.db.session import session_scope
from atlas.retrieval.tools import AtlasTools
from atlas.retrieval.verify import ANSWER_SCHEMA, render, verify

MAX_STEPS = 24
MAX_SUBMISSIONS = 3
RETRIES = 4
RETRY_SECONDS = 2
_RESULT_LIMIT = 30000

SYSTEM = """You answer questions about the user's work from their emails, meeting transcripts and documents,
using tools. You know nothing about these people, companies or projects except what the tools return.

Tools:
- find: resolve a name ("Sarah", "Acme", "the Darwin standup") to graph records and their ids. If several
  are close and the question does not make clear which, ask the user instead of answering (submit_answer
  with clarify, naming the candidates).
- interactions, contacts, participants: the relationship graph, exact over every email and meeting. Take
  counts, dates, who wrote to or met whom, first and last contact, and who attended from these, never by
  counting search results.
- search, read: the text itself, for what was said, proposed or decided. Search returns a sample of
  passages; search more than once with different words when needed, and read a whole meeting or document
  to see who said what.

Answering (submit_answer):
- One part per claim: a sentence or bullet. Every factual part cites the refs it rests on. For a text ref
  (S#) give the exact words from that passage that support the claim; for a graph ref (G#) the record is
  the evidence.
- Say only what the cited evidence says: no general knowledge, no speculation, no filler, no advice.
  Attribute statements to the person the transcript or email shows saying them.
- Keep what answers the question. Leave out incidental details (phone numbers, extensions, office
  locations, equipment, signatures) unless the question asks for them.
- If the sources do not answer the question, say so in one short uncited part.
- The answer is verified. Parts whose quotes are not in the cited passage, or whose numbers and dates are
  not in the cited evidence, come back: fix them with the right evidence or drop them, then resubmit.

Today is {today}. {asker}"""


def _fit(result: dict, limit: int = _RESULT_LIMIT) -> str:
    """A tool result as valid JSON of at most about `limit` characters. Whole items come off
    the end of the longest list (the lowest-ranked passages or oldest records) and the result
    says how many, so the model knows to narrow the query instead of reading broken JSON."""
    text = json.dumps(result, default=str)
    if len(text) <= limit:
        return text
    result, dropped = dict(result), 0
    while len(text) > limit:
        lists = [k for k, v in result.items() if isinstance(v, list) and v]
        if not lists:
            break
        longest = max(lists, key=lambda k: len(json.dumps(result[k], default=str)))
        result[longest] = result[longest][:-1]
        dropped += 1
        result |= {"truncated": True,
                   "note": f"{dropped} items left out to fit; narrow the query or lower the limit to see the rest"}
        text = json.dumps(result, default=str)
    if len(text) > limit:                           # no list left to trim: keep the short fields only
        text = json.dumps({**{k: v for k, v in result.items() if isinstance(v, (int, float, bool)) or
                              (isinstance(v, str) and len(v) <= 200)},
                           "truncated": True, "note": "the result was too large to show; narrow the query"},
                          default=str)
    return text


def _client(settings: Settings):
    from openai import OpenAI

    return OpenAI(api_key=settings.openai_api_key)


def _complete(client, **kwargs):
    """One model turn, retried on transient API errors (rate limits, timeouts, 5xx)."""
    import openai

    for attempt in range(RETRIES):
        try:
            return client.chat.completions.create(**kwargs)
        except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError,
                openai.InternalServerError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(RETRY_SECONDS * 2 ** attempt)


def answer(question: str, settings: Settings | None = None, asker: str | None = None, client=None,
           brain=None) -> dict:
    settings = settings or get_settings()
    client = client or _client(settings)
    t0 = time.perf_counter()
    trace, submissions, usage = [], 0, {"prompt_tokens": 0, "completion_tokens": 0}
    final = None
    with session_scope() as s:
        tools = AtlasTools(s, settings, asker_email=asker, brain=brain)
        who = (f"The person asking is {tools.asker['name']} <{tools.asker['email']}> (id {tools.asker['id']}); "
               "\"I\", \"me\", \"we\" and \"you\" in the question mean them." if tools.asker else "")
        messages = [{"role": "system", "content": SYSTEM.format(today=date.today().isoformat(), asker=who)},
                    {"role": "user", "content": question}]
        for _ in range(MAX_STEPS):
            response = _complete(client, model=settings.agent_model, messages=messages,
                                 tools=tools.schemas + [ANSWER_SCHEMA], tool_choice="required")
            if response.usage:
                usage["prompt_tokens"] += response.usage.prompt_tokens
                usage["completion_tokens"] += response.usage.completion_tokens
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls or []:
                args = json.loads(call.function.arguments or "{}")
                if call.function.name == "submit_answer":
                    submissions += 1
                    report = verify(args.get("parts") or [], tools.ledger)
                    trace.append({"tool": "submit_answer", "accepted": report["accepted"],
                                  "failed": len(report["failed"])})
                    if args.get("clarify") or report["accepted"] or submissions >= MAX_SUBMISSIONS:
                        final = (args, report)
                        result = {"accepted": True}
                    else:
                        result = {"accepted": False, "failed": report["failed"],
                                  "instruction": "Fix these parts with the right evidence or drop them, then resubmit."}
                else:
                    result = tools.call(call.function.name, args)
                    trace.append({"tool": call.function.name, "args": args})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _fit(result)})
            if final:
                break
        out = {"question": question, "mode": "agent", "route": "agent", "route_reason": None, "error": None,
               "entities": [], "facts": None, "answer": "", "citations": [], "dropped": [], "clarify": None,
               "tool_calls": trace, "tokens": usage}
        if final is None:
            out["error"] = f"no answer after {MAX_STEPS} steps"
        else:
            args, report = final
            if args.get("clarify"):
                out |= {"route": "clarify", "answer": args["clarify"], "clarify": args["clarify"]}
            else:
                out["answer"], out["citations"], out["dropped"] = render(args.get("parts") or [], report, tools.ledger)
                out["verification"] = {"parts": report["parts"], "failed": len(report["failed"]),
                                       "submissions": submissions}
    out["seconds"] = round(time.perf_counter() - t0, 1)
    return out
