# Atlas: a knowledge graph on Postgres + pgvector

> **V1 (current direction):** `atlas/` is a deterministic business knowledge graph compiler driven by a versioned
> ontology (`ontology/v1_0/`). Extractors only propose; the compiler decides, and every edge carries evidence. See
> [docs/atlas_v1.md](docs/atlas_v1.md). Quick start:
> `uv run alembic upgrade head && uv run python -m atlas ingest examples/business && uv run python -m atlas edges`.
>
> The rest of this README describes the **prototype** (`brain/`, tag `prototype-v0`): LLM extraction with
> deterministic resolution, plus the evaluation harnesses we used to decide on V1.

## Prototype: brain

The pipeline turns documents into a knowledge graph:

1. Documents are chunked.
2. An LLM proposes entities and relationships for each chunk, as structured output.
3. Entity resolution runs deterministically and decides canonical identity.
4. Relationships are validated, and each accepted one is stored with evidence.

Everything is stored in PostgreSQL: relational node/edge tables for the graph, and pgvector for embeddings. It needs no Neo4j and no separate vector database.

The design borrows the good parts of [Cognee](https://github.com/topoteretes/cognee)'s `cognify` pipeline without depending on it:
- [docs/cognee_pipeline_analysis.md](docs/cognee_pipeline_analysis.md) traces Cognee's pipeline and explains what was reused.
- [docs/architecture.md](docs/architecture.md) describes this system.

## Quick start (fresh clone → first graph)

Prerequisites:
- Docker
- [uv](https://docs.astral.sh/uv/)
- an OpenAI API key

```bash
# 1. Postgres 16 + pgvector on localhost:5433
docker compose up -d

# 2. Python deps (uv downloads a suitable Python if needed)
uv sync

# 3. Config: then edit .env and set OPENAI_API_KEY
cp .env.example .env

# 4. Schema (extensions vector + pg_trgm, all tables and indexes)
uv run alembic upgrade head

# 5. Load the sample corpus (offline: parse + chunk only)
uv run python -m brain ingest examples/corpus

# 6. Embed + extract + resolve + persist
uv run python -m brain build

# 7. Look around
uv run python -m brain graph "KRAS G12C"
```

`uv run` runs a command inside the project's virtualenv. If you activate `.venv` yourself, the commands are the same without the prefix, for example `python -m brain build`.

## CLI

```bash
uv run python -m brain ingest examples/corpus        # idempotent; changed files replace their chunks
uv run python -m brain build                         # cached per chunk; --force re-extracts everything
uv run python -m brain stats
uv run python -m brain entities                      # --type Drug, --ambiguous
uv run python -m brain relationships --evidence      # --type targets
uv run python -m brain search "What compounds target KRAS G12C?"   # hybrid; --chunks-only, --json
uv run python -m brain ask "Which trial supported the approval of Lumakras?"   # LLM answer with cited sources; --mode vector
uv run python -m brain graph "Sotorasib"             # --depth 2
uv run python -m brain why "Sotorasib" "KRAS G12C"   # evidence behind the edge(s)
uv run python -m brain path "Amgen" "KRAS"           # shortest paths, max 3 hops
uv run python -m brain reset                         # drop the graph, keep documents (--all for everything)
uv run python -m brain reset --keep-extractions -y && uv run python -m brain build --replay
                                                     # re-run resolution/validation on cached LLM output
```

## Consistency experiment

Run the same corpus N times from an empty graph and measure how stable the result is:

```bash
uv run python scripts/evaluate_consistency.py                                  # Experiment A: dynamic types
uv run python scripts/evaluate_consistency.py --ontology examples/ontology.json # Experiment B: constrained types
uv run python scripts/evaluate_consistency.py --from-dir runs/<ts>             # recompute metrics only
```

- It uses a separate database, `brain_eval`, so your main graph is untouched.
- Output goes to `runs/<timestamp>/`:
  - `run_01.json` … `run_10.json`: entities, edges with evidence, resolver decisions, and the raw LLM output for each chunk.
  - `summary.json`
  - `report.md`

Cognee baseline (same corpus and model, run in a separate venv so Cognee never enters this project's dependencies):

```bash
uv venv references/cognee-venv --python 3.12
VIRTUAL_ENV=references/cognee-venv uv pip install -e references/cognee
references/cognee-venv/bin/python scripts/cognee_baseline.py --runs 10
uv run python scripts/evaluate_consistency.py --from-dir runs/cognee_baseline
uv run python scripts/evaluate_consistency.py --from-dir runs/experiment_a --raw   # our raw LLM output, unresolved
```

Results from the 10-run experiments (A: dynamic, B: ontology, raw output, Cognee baseline) are in [docs/consistency_results.md](docs/consistency_results.md).

## Question-answering evaluation

26 questions with reference answers are in `examples/qa/questions.json`. Answers are graded blind by a judge model. The evaluation compares `brain ask` (hybrid), vector-only RAG, and Cognee's search modes:

```bash
references/cognee-venv/bin/python scripts/cognee_qa.py --out runs/qa/cognee_answers.json   # optional Cognee arm
uv run python scripts/evaluate_qa.py --cognee-answers runs/qa/cognee_answers.json
```

Results are in [docs/qa_results.md](docs/qa_results.md).

## Tests

```bash
uv run pytest
```

- The LLM is mocked in tests, and embeddings use the offline `HashEmbedder`.
- DB tests run against a separate `brain_test` database, created and migrated automatically. They need `docker compose up -d`.

## Configuration

All settings are environment variables; see `.env.example` and `brain/config.py`.

| variable | default | notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://brain:brain@localhost:5433/brain` | port 5433 avoids clashing with a local Postgres |
| `OPENAI_API_KEY` | — | required for `build` / `search` with OpenAI |
| `LLM_MODEL` | `gpt-4o-mini` | any model that supports structured outputs |
| `LLM_TEMPERATURE`, `LLM_SEED` | `0`, unset | still not fully deterministic, which is why the eval exists |
| `EMBEDDING_PROVIDER` | `openai` | `hash` = offline lexical embedder, no key needed |
| `EMBEDDING_MODEL`, `EMBEDDING_DIM` | `text-embedding-3-small`, `1536` | the dimension is baked into the migration |
| `CHUNK_MAX_TOKENS` | `400` | |
| `ALLOWED_ENTITY_TYPES`, `ALLOWED_RELATIONSHIP_TYPES` | `[]` | JSON lists; non-empty = Experiment B |
| `RESOLUTION_FUZZY_THRESHOLD` | `92` | rapidfuzz ratio |
| `RESOLUTION_EMBEDDING_ENABLED` | `false` | embedding-assisted entity resolution |
| `EVIDENCE_MIN_GROUNDING` | `90` | how closely the evidence quote must match the chunk |
| `EVIDENCE_REQUIRE_ENDPOINTS_IN_QUOTE` | `false` | strict mode: both entities must be named in the quote |
| `RELATIONSHIP_TYPE_SYNONYMS`, `RELATIONSHIP_TYPE_INVERSES` | `{}` | JSON maps for relation normalization |

## Layout

```
brain/                  the service (see docs/architecture.md)
migrations/             Alembic
scripts/                evaluate_consistency.py
examples/corpus/        synthetic sample documents (aliases + restated facts on purpose)
examples/ontology.json  type allow-lists for Experiment B
tests/                  pytest suite (mocked LLM)
references/cognee/      reference clone (git-ignored; re-create with the command below)
docs/                   architecture + Cognee analysis
```

Re-create the Cognee reference checkout:

```bash
git clone --depth 1 https://github.com/topoteretes/cognee references/cognee
```
