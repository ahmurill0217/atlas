# Atlas

Atlas is a knowledge graph over a company's email, documents and meetings (Gmail, Drive, Fathom). It is built from metadata alone, without a model. It is paired with [brain](../brain), which provides cited retrieval over the same documents. Together they answer questions like:

- "What do I know about Sarah Chen?"
- "Who at Acme have we dealt with, and when did we last talk?"
- "Prep me for a call with Tom."

`atlas ask` gives a model (gpt-5.5 by default) tools over both, and checks its answer before showing it:

| Tool | What it answers |
|---|---|
| `find` | who or what a name means ("Sarah", "bofa", "the Darwin standup"), ranked by who you deal with |
| `interactions`, `contacts`, `participants` | the graph, exact over every email and meeting: who, with whom, how often, first and last, who attended |
| `search`, `read` | the text: what was said, proposed or decided, and by whom |

- **Every claim cites a tool result:** G# for a graph record, S# for a passage of text, with the exact words quoted.
- **Every answer is verified** before it is shown: quotes must appear in the cited passage, and numbers and dates in the cited evidence. Parts that fail go back to the model; what still fails is removed and reported.
- **Ambiguous names get a question back** ("Which Sarah do you mean?"), not a guess.

```
$ atlas ask "When did Phillip Allen last email Keith Holst, and what was it about?"
Phillip Allen's last email to Keith Holst was on 2001-05-07; the email was titled
"California Update 5/4/01" [G9] [G14].
The email was a forwarded California update from Kristin Walsh ... [S1].

Sources:
  [G9]  California Update 5/4/01 (2001-05-07)  e63586cb-...  (graph)
  [S1]  California Update 5/4/01 (2001-05-07)  e63586cb-...  (search)
        "If you have any questions, please contact Kristin Walsh"
```

A platform with its own agent uses the same pieces as a library: `AtlasTools` (`atlas/retrieval/tools.py`), `ANSWER_SCHEMA`, `verify` and `render` (`atlas/retrieval/verify.py`).

## Status

Tested on 5,000 Enron emails, four work documents and one 70-minute meeting, about 60 questions (`docs/ask_evaluation.md`).

- **The agent beats the earlier router on every set.** It gets relationship questions exactly, answers content questions without filler, and handles meetings (the router couldn't).
- **Time:** simple questions take about 5–7 s; call prep takes about 35 s (up to 75 s).

**Next:** a pilot on real Gmail, Drive and Fathom data with real questions.

**Not built yet:**
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
atlas ask → model calls graph tools [G#] and search [S#] → answer → verifier → cited answer
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
- `--mode` accepts `agent` (the default). The earlier router is kept as a baseline: `auto`, `relationship` (graph only), `content` (brain with the graph profile) and `baseline` (brain alone).

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

## Your own Gmail (Takeout)

Export your mail from [takeout.google.com](https://takeout.google.com):
1. Click "Deselect all", then tick Mail.
2. Export once as a .zip and unzip it.

The file is `Takeout/Mail/All mail Including Spam and Trash.mbox`.

```bash
mkdir -p runs/gmail
uv run python scripts/takeout_to_atlas.py "path/to/All mail Including Spam and Trash.mbox" runs/gmail/json \
  --since 2025-01-01 --limit 5000 --attachments runs/gmail/files

export DATABASE_URL=postgresql+psycopg://brain:brain@localhost:5433/brain_gmail
export ATLAS_INTERNAL_DOMAINS='[]' ATLAS_BRAIN_INDEX=atlas_gmail   # a company account: its domains
uv run python -c "from atlas.db.admin import ensure_database, migrate; import os; \
  ensure_database(os.environ['DATABASE_URL']); migrate(os.environ['DATABASE_URL'])"
uv run python -m atlas ingest runs/gmail/json
uv run python -m atlas ingest runs/gmail/files        # saved PDF / DOCX attachments
uv run python -m atlas index
uv run python -m atlas ask "Who have I emailed most this year?" --as you@gmail.com
```

- **Skipped:** Spam, Trash, Chats, Promotions and Social (`--keep-promotions` keeps the last two).
- **`--limit`** keeps the most recent messages.
- **Where your data goes:** everything stays on this machine (`runs/` is git-ignored) except answering. The question, graph facts and retrieved email text go to the answer model (OpenAI).

## Layout

| Path | What |
|---|---|
| `atlas/ingestion` | source adapters, normalized documents, segmentation |
| `atlas/extraction`, `compiler`, `resolution`, `ontology` | metadata → candidates → the only path into the graph |
| `atlas/graph`, `review`, `provenance` | storage, queries, viewer, review queue, audit and evidence |
| `atlas/retrieval` | brain indexing, the tools, the verifier, the answer loop (`agent.py`); the earlier router |
| `ontology/` | the ontology (versioned in its files; `CHANGELOG.md`) |
| `scripts/` | Enron and Gmail Takeout converters, sample generator, ask evaluator, citation audit |
| `docs/` | architecture, reference, evaluation, findings |
