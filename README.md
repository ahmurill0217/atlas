# Atlas

Atlas is a knowledge graph over a company's email, documents and meetings (Gmail, Drive, Fathom). It is built from metadata alone, without a model. It is paired with [brain](../brain), which provides cited retrieval over the same documents. Together they answer questions like:

- "What do I know about Sarah Chen?"
- "Who at Acme have we dealt with, and when did we last talk?"
- "Prep me for a call with Tom."

`atlas ask` sends each question to the part that can answer it:

| Question | Example | Answered by |
|---|---|---|
| Relationship | who, how often, when, who at a company | the graph (exact, over every email) |
| Content | what was said, proposed, decided | brain (search + LLM) |
| Mixed | call prep, "who is X and what do they work on" | both, in two labelled sections |

- **Names as people write them:** "Sarah" becomes the Sarah you email most, and the answer says so.
- **No guessing on ambiguous names:** if two Sarahs are close, `atlas ask` returns the candidates as a clarifying question (route `clarify`).

Every claim cites the email it comes from:

```
$ atlas ask "When did Phillip Allen last email Keith Holst, and what was it about?"
Route: mixed

**From the relationship graph** (email metadata, every email)
- Last email from Phillip Allen to Keith Holst: 2001-05-07 [G23].

**From the emails**
The last email that Phillip Allen sent to Keith Holst was on May 7, 2001, with the subject
"California Update 5/4/01" ... [[7]]()

Sources:
  [G23] California Update 5/4/01 (2001-05-07)  e63586cb-...  (graph)
  [7]   California Update 5/4/01 (2001-05-07)  e63586cb-...  (search)
```

## Status

The system has been tested on 5,000 Enron emails.

- **Content questions:** brain answers well.
- **Relationship questions:** the graph answers exactly; plain retrieval can't count.
- **Mixed questions:** the combination beats plain retrieval.
- **Citations:** 81% of cited claims are backed by the cited email, and every graph citation is.

**Next:** a pilot on real Gmail, Drive and Fathom data with real questions.

**Not built yet:**
- checking citations at answer time;
- reconstructing deals ("how did we close this deal");
- linking the people and companies mentioned inside document text.

Details: [docs/architecture.md](docs/architecture.md) and [docs/ask_evaluation.md](docs/ask_evaluation.md).

## How it works

```
source (email / meeting / Drive JSON, PDF, DOCX, text)
  │ adapters → normalized document → sections
  ├─► Atlas graph (Postgres, schema kg)       who wrote to whom, when; meetings, action items, authors
  │     ontology-checked, one person across addresses, evidence on every fact, review queue, audit log
  └─► brain index (OpenSearch)                each document's own text, headed by From/To/Date/Subject
                     shared key: document_id
atlas ask → graph facts [G#] and/or brain retrieval [n] → cited answer
```

- The ontology in `ontology/` is the only schema. Nothing outside it can enter the graph.
- Anything ambiguous goes to a review queue instead of being guessed.
- `atlas why <edge-id>` shows the evidence and decisions behind any fact.

## Requirements

- Docker, [uv](https://docs.astral.sh/uv/) and Python 3.13
- [brain](../brain) checked out next to this repo (`../brain`); it is an editable path dependency
- An OpenAI API key, for answering only. Building the graph uses no model.

## Setup

```bash
docker compose up -d                  # Postgres (pgvector image) on host port 5433
uv sync
uv run alembic upgrade head
cp .env.example .env                  # set OPENAI_API_KEY and ATLAS_INTERNAL_DOMAINS (your email domains)
(cd ../brain && docker compose up -d) # brain's OpenSearch (9201) and embedding model server (9100)
```

## Use

```bash
uv run python -m atlas ingest examples/business          # documents -> graph (no model; re-runs are no-ops)
uv run python -m atlas index                             # documents -> brain
uv run python -m atlas ask "Who at Acme have we dealt with?" --as you@yourco.com
uv run python -m atlas entity sarah.chen@acme.com        # an entity, its identifiers and edges
uv run python -m atlas why <edge-id>                     # evidence -> candidates -> decision -> audit
uv run python -m atlas view sarah.chen@acme.com --out graph.html   # interactive graph view
uv run python -m atlas reviews                           # what the compiler would not decide
uv run python -m atlas resolve <review-id> --status REJECTED --reviewer "Your Name" --note "Why"
uv run python -m atlas stats                             # unsupported_edges must be 0
uv run python -m atlas ontology --details
```

- `--as <email>` says who is asking, so "I", "we" and "you" mean someone.
- `--mode` accepts `auto` (the default), `relationship` (graph only), `content` (brain with the graph profile) or `baseline` (brain alone).

**Input formats:**
- email JSON (Gmail-style headers);
- Fathom meeting JSON;
- generic "Atlas document" JSON (Drive metadata plus text);
- PDF, DOCX, Markdown and plain text.

See [docs/reference.md](docs/reference.md).

## Tests

```bash
uv run pytest
```

The tests run against a separate `<db>_test` database, created and migrated automatically. They make no model calls.

## Enron test corpus

```bash
mkdir -p runs/enron
curl -L -o runs/enron/train-00000.parquet \
  https://huggingface.co/datasets/corbt/enron-emails/resolve/main/data/train-00000-of-00003.parquet
uv run python scripts/enron_to_atlas.py runs/enron/train-00000.parquet runs/enron/json_5k \
  --mailboxes allen-p,arnold-j,grigsby-m,griffith-j,cuilla-m,ermis-f --limit 5000

export DATABASE_URL=postgresql+psycopg://brain:brain@localhost:5433/brain_enron
export ATLAS_INTERNAL_DOMAINS='["enron.com"]' ATLAS_BRAIN_INDEX=atlas_enron
uv run python -c "from atlas.db.admin import ensure_database, migrate; import os; \
  ensure_database(os.environ['DATABASE_URL']); migrate(os.environ['DATABASE_URL'])"
uv run python -m atlas ingest runs/enron/json_5k          # about 5 minutes
uv run python -m atlas index                              # slow on CPU; a GPU or hosted embeddings at scale

uv run python scripts/evaluate_ask.py examples/qa/enron_mixed_questions.json runs/enron/ask/mixed.json
uv run python scripts/audit_citations.py runs/enron/ask/mixed.json --mode auto
```

The question sets are in `examples/qa/`: content, relationship and mixed.

## Layout

| Path | What |
|---|---|
| `atlas/ingestion` | source adapters, normalized documents, segmentation |
| `atlas/extraction`, `compiler`, `resolution`, `ontology` | metadata → candidates → the only path into the graph |
| `atlas/graph`, `review`, `provenance` | storage, queries, viewer, review queue, audit and evidence |
| `atlas/retrieval` | brain indexing, graph facts for questions, the router |
| `ontology/` | the ontology (versioned in its files; `CHANGELOG.md`) |
| `scripts/` | Enron converter, sample generator, ask evaluator, citation audit |
| `docs/` | architecture, reference, evaluation, findings |
