# Atlas

Atlas is a knowledge graph over a company's email, documents and meetings, built from metadata without a model. It is paired with [brain](../brain), which provides cited retrieval over the same documents.

`atlas ask` sends each question to the part that can answer it:
- **Relationship questions** (who, how often, when, who at a company) go to the graph.
- **Content questions** (what was said) go to brain.
- **Mixed questions** go to both.

Every answer cites the emails behind it.

- **How it fits together:** [docs/architecture.md](docs/architecture.md)
- **Ingestion, ontology and compiler reference:** [docs/atlas_v1.md](docs/atlas_v1.md)
- **Evaluation:** [docs/ask_evaluation.md](docs/ask_evaluation.md)
- **Experiments behind the design:** [docs/findings.md](docs/findings.md)

## Setup

```bash
docker compose up -d                 # Postgres on host port 5433
uv sync
uv run alembic upgrade head
cp .env.example .env                 # set OPENAI_API_KEY and ATLAS_INTERNAL_DOMAINS
```

Retrieval needs brain's stack (OpenSearch on 9201, embedding model server on 9100). Start it with `docker compose up -d` in `../brain`.

## Use

```bash
uv run python -m atlas ingest examples/business          # metadata -> graph, no model
uv run python -m atlas index                             # documents -> brain
uv run python -m atlas ask "Who at Acme have we dealt with?" --as you@yourco.com
uv run python -m atlas entity sarah.chen@acme.com        # an entity and its edges
uv run python -m atlas why <edge-id>                     # evidence and decisions behind an edge
uv run python -m atlas view sarah.chen@acme.com --out graph.html
uv run python -m atlas reviews                           # what the compiler would not decide
uv run python -m atlas stats
uv run pytest
```

`atlas ask --mode` accepts `auto` (the default), `relationship`, `content` or `baseline` (brain alone).

## Enron test corpus

```bash
uv run python scripts/enron_to_atlas.py <enron parquet> runs/enron/json_5k --limit 5000
DATABASE_URL=.../brain_enron ATLAS_INTERNAL_DOMAINS='["enron.com"]' ATLAS_BRAIN_INDEX=atlas_enron \
  uv run python -m atlas ingest runs/enron/json_5k
uv run python scripts/evaluate_ask.py examples/qa/enron_mixed_questions.json runs/enron/ask/mixed.json
uv run python scripts/audit_citations.py runs/enron/ask/mixed.json --mode auto
```

The data comes from Hugging Face `corbt/enron-emails`.
