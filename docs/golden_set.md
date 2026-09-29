# Golden set: scoring `atlas ask` automatically

A golden set is a list of questions, each saying what a correct answer must contain. `scripts/score_ask.py` grades saved answers against it with plain string and set checks: no model, no database. It replaces hand grading for everything that has a checkable answer, and it can gate CI.

The scorer is `atlas/retrieval/golden.py`. `examples/qa/golden_template.json` has one question of each kind over the sample corpus in `examples/business`.

## Running it

```
# 1. Ask each question 3 times (answers are saved; the run can be resumed)
uv run python scripts/evaluate_ask.py examples/qa/golden.json runs/golden/results.json --modes agent --runs 3

# 2. Score them (fast; rerun as often as you like)
uv run python scripts/score_ask.py examples/qa/golden.json runs/golden/results.json --out runs/golden/scores.json
```

`score_ask.py` prints pass rates overall, by source, by type and by check. It then lists the **failing** questions (every run failed) and the **flaky** ones (some runs failed), with the reason for each failed run. `--min-pass 0.85` makes it exit 1 below that rate.

Each question's pass rate is the share of its runs that passed. One run can pass or fail by chance, so use `--runs 3` or more for any decision.

## Question format

```json
{
  "id": "gm-rel-001",
  "source": "gmail",
  "type": "relationship",
  "as": "mike.rodriguez@northwind.io",
  "question": "How many emails has Sarah Chen sent me, and when was the last one?",
  "expect": {
    "facts": [["2", "two"], "2026-09-03"],
    "tools": ["interactions"]
  },
  "key_points": ["2 emails; last 2026-09-03 (Re: Atlas Rollout)"],
  "notes": "Counts must come from the graph."
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique. A prefix by source and type (`gm-rel-`, `dr-con-`, `fa-con-`, `mx-`, `un-`, `cl-`) keeps the file sortable. |
| `question` | yes | Word it the way a user would ask. |
| `type` | yes | `relationship`, `content`, `mixed`, `unanswerable` or `clarify`. |
| `source` | no | `gmail`, `drive`, `fathom` or `mixed`. Used to split pass rates. |
| `as` | no | Email of the asker, so "I", "me" and "we" resolve. |
| `expect` | no | The checks below. With none, the question is *ungraded*. |
| `key_points` | no | The full expected answer in prose, for a person or an LLM judge. |
| `notes` | no | Why the question exists, and what it tests. |

### Checks (`expect`)

Only the checks you give are scored. A question passes when all of them pass.

| Check | Passes when |
|---|---|
| `facts: [...]` | The answer contains every item. An item can be a list of alternatives (`["2", "two"]`). Case and punctuation are ignored, and matching is on word boundaries, so `86` doesn't match `186`. An ISO date such as `2001-05-07` also matches `May 7, 2001`, `7 May 2001`, `May 7 2001` and `5/7/2001`, and short month names (`Sep 3, 2026`). |
| `absent: [...]` | The answer contains none of these. Use it for the likely wrong answer: another person's name, a figure from a similar document. |
| `documents: [...]` | The answer cites these documents, each given as an Atlas document id or its exact title. `documents_match: "any"` passes on one of them; the default is all. |
| `tools: [...]` | The agent called these tools. Use `interactions` or `contacts` for counts and dates, so a lucky guess from search doesn't pass. |
| `clarify: true/false` | The answer is (or is not) a question back, like "Which Sarah do you mean?". Required for type `clarify`. |
| `unanswerable: true` | The answer cites nothing and doesn't ask a question back. Required for type `unanswerable`. |

Prefer titles over ids in `documents`. They're readable, and they survive re-ingestion: a file's document id is derived from the path it was ingested from.

A clarify example (needs two people named Sarah in the corpus):

```json
{"id": "cl-001", "source": "mixed", "type": "clarify", "question": "When did I last talk to Sarah?",
 "as": "mike.rodriguez@northwind.io",
 "expect": {"clarify": true, "facts": ["Sarah Chen", "Sarah Kim"]}}
```

## Writing good checks

- **Check the fact, not the phrasing.** Use `"2026-09-03"` or `"120,000"`, not a sentence. A short, exact fact is hard to pass by accident and doesn't fail on a correct paraphrase.
- **Add alternatives only for real variants.** Numbers written out (`two`) and units (`120k`) are real variants. Dates are handled for you.
- **Use `absent` for the tempting wrong answer.** It catches a model that grabs the nearest similar number or person.
- **Every graph question gets `tools`.** Otherwise a count that happens to match a search sample passes.
- **Keep `key_points` too.** The checks catch most errors. `key_points` is what a person or a judge reads for everything else: tone, completeness, extra claims.

## What the checks can't tell you

The checks tell you whether the right facts, sources and tools are present. They can't tell you whether the answer is well organized, whether it adds unsupported claims (the verifier in `atlas/retrieval/verify.py` covers most of those), or whether a summary is complete. For those, hand-grade a sample of passing answers now and then, or add an LLM judge that you've checked against your own grades.

## Trap corpus: persistent-knowledge-layer

[persistent-knowledge-layer](https://github.com/mcekikj/persistent-knowledge-layer) (MIT) ships 21 made-up insurance documents written so that plain retrieval gives a confident wrong answer. They contain six traps: a rule that applies only in one scope, two current documents that contradict each other, several names for one concept, rules with effective dates, a rationale that exists only in an email, and questions that need several documents. `examples/qa/pkl_trap_questions.json` asks about each trap.

```
git clone https://github.com/mcekikj/persistent-knowledge-layer ../persistent-knowledge-layer
uv run python scripts/pkl_to_atlas.py ../persistent-knowledge-layer runs/pkl/corpus

# a separate database and index, so the trap corpus doesn't mix with real data
export DATABASE_URL=postgresql+psycopg://brain:brain@localhost:5433/atlas_pkl ATLAS_BRAIN_INDEX=atlas_pkl
uv run python -c "from atlas.db.admin import ensure_database, migrate; import os; \
  ensure_database(os.environ['DATABASE_URL']); migrate(os.environ['DATABASE_URL'])"
uv run atlas ingest runs/pkl/corpus
uv run atlas index                     # the brain index (see the README)
uv run python scripts/evaluate_ask.py examples/qa/pkl_trap_questions.json runs/pkl/results.json --modes agent --runs 3
uv run python scripts/score_ask.py examples/qa/pkl_trap_questions.json runs/pkl/results.json
```

The corpus has names but no email addresses, so its graph is thin. Each email's sender and recipients are linked, but a second mention of a name-only person goes to the review queue instead of being merged. The run mostly tests search, the agent and the verifier. Two traps test behavior the checks can only partly see, so read those answers by hand: `pkl-conflict-001` (does it present both sides without choosing one?) and `pkl-scope-001` (is 15 years presented as the general rule?).
