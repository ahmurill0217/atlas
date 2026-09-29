# Architecture

This is a small knowledge "brain". Documents go in, and a knowledge graph with mandatory provenance comes out, stored entirely in **PostgreSQL + pgvector**. The design takes ideas from Cognee's `cognify` pipeline ([cognee_pipeline_analysis.md](cognee_pipeline_analysis.md)). The main difference: **the LLM only proposes; the application decides identity, deduplication and persistence.**

## Data flow

```
 python -m brain ingest <dir>                 (offline, deterministic, idempotent)
 ───────────────────────────
 loader.discover_files  →  parser.parse_file  →  chunker.chunk_text
   sorted .txt/.md          NFC, newline norm,     paragraph → sentence packing,
                            sha256, title          token budget, char offsets
            ↓
   documents (id = uuid5(source_uri), content_hash)   ── unchanged hash → skip
   chunks    (id = uuid5(doc, index, sha256(text)))   ── changed hash  → replace chunks

 python -m brain build                        (LLM + embeddings)
 ─────────────────────
 1. embed chunks lacking an embedding (or embedded by another model) ─► chunks.embedding (HNSW)
 2. pending chunks = no chunk_extractions row for cache_key(model, prompt, ontology)
 3. KnowledgeGraphExtractor.extract (thread pool)
        StructuredLLM.generate(system, user, ExtractedKnowledgeGraph)   ← OpenAI structured outputs
        clean_extraction: merge same-name entities, drop dangling local ids (logged)
 4. per chunk, in (document, chunk_index) order, ONE transaction:
        EntityResolver     → MATCH_EXISTING | CREATE_NEW | AMBIGUOUS (+ aliases, mention, type vote, log)
        RelationshipValidator → accept | reject(reason)                (log)
        upsert edge on UNIQUE(source, target, type) → attach evidence (UNIQUE(edge, chunk)) → support_count
        store raw extraction in chunk_extractions (cache + audit)
 5. prune edges without evidence and entities without mentions; finalize extraction_runs row
```

## Package layout

```
brain/
  config.py                 all knobs (env / .env): models, chunking, thresholds, ontology
  normalize.py              name / type / relation normalization (identity starts here)
  cli.py, __main__.py       Typer CLI
  db/                       engine + session, ORM models, admin (create/migrate/truncate DBs)
  ingestion/                loader.py (file discovery), parser.py (text + hash)
  chunking/                 chunker.py, models.py
  llm/                      StructuredLLM protocol + OpenAI implementation
  embeddings/               Embedder protocol, OpenAIEmbedder, HashEmbedder (offline)
  extraction/               models.py (IR), prompts.py, extractor.py
  resolution/               entity_resolver.py, relationship_resolver.py (validation)
  graph/                    repository.py (writes), traversal.py (SQL reads), models.py (views)
  pipeline/                 ingest.py, build_brain.py
  search/                   semantic.py (pgvector), graph.py, hybrid.py
  evaluation/               consistency.py (metrics), snapshot.py (graph export)
scripts/evaluate_consistency.py
migrations/                 Alembic (0001_initial_schema.py is hand-written SQL)
```

This differs from the suggested layout in three ways:
- **`llm/` and `normalize.py`** were added so the provider interface and string normalization each live in exactly one place.
- **ORM tables live in `db/models.py`** instead of `graph/models.py`. `graph/models.py` holds read-side view dataclasses.
- **`evaluation/`** was added.

## Schema

Everything is in one database. `migrations/versions/0001_initial_schema.py` is the DDL source of truth.

| table | purpose | key constraints / indexes |
|---|---|---|
| `documents` | source files | `UNIQUE(source_uri)`, index on `content_hash` |
| `chunks` | text units + `embedding VECTOR(1536)` | `UNIQUE(document_id, chunk_index)`, **HNSW (cosine)** |
| `entities` | canonical nodes | index on `normalized_name`, **trigram GIN** on `normalized_name`, index on `entity_type`, HNSW on `embedding` (for optional resolution) |
| `entity_aliases` | every surface form (incl. the canonical name) | `UNIQUE(entity_id, normalized_alias)`, index on `normalized_alias` |
| `relationships` | canonical edges, `support_count`, `confidence` (max of evidence) | **`UNIQUE(source, target, type)`** (also serves source lookups), index on target, index on type |
| `entity_mentions` | entity ↔ chunk, with offsets | `UNIQUE(entity, chunk, mention_text)`, index on chunk |
| `relationship_evidence` | quote + chunk + run supporting an edge | **`UNIQUE(relationship, chunk)`**, index on chunk |
| `extraction_runs` | model, prompt version, config, stats per build | |
| `chunk_extractions` | raw LLM output per chunk (cache + audit) | `UNIQUE(chunk, cache_key)` |
| `resolution_log` | every entity / relationship decision, with method, score and candidates | index on (run, kind, decision) |

**Invariants:**
- Every relationship has at least one evidence row. `prune_unsupported` enforces this after documents change.
- Every entity has at least one mention.
- Re-running `build` or `ingest` never duplicates aliases, mentions, edges or evidence, because all of those writes are `INSERT … ON CONFLICT`.

## Entity resolution (`resolution/entity_resolver.py`)

Resolution steps run in order, and the first step with a hit decides. Within a step, several distinct candidates count as **AMBIGUOUS** unless the entity type narrows them to one.

| step | how | default |
|---|---|---|
| 1. exact | `normalize_name(name)` = `entities.normalized_name` (NFKC, casefold, punctuation → space, possessive stripped) | on |
| 2. alias | first the extracted **name** vs. `entity_aliases`; only if that finds nothing, the extracted (safe) **aliases**. The name is the LLM's primary claim, and its aliases are sometimes wrong: "Like Lumakras, Krazati binds…" produced Krazati with the alias *Lumakras*. | on |
| 3. fuzzy | (a) whitespace-free key equal (`AMG510` ≈ `AMG 510`); (b) pg_trgm candidates scored with `rapidfuzz.ratio ≥ 92`, **same type**, **identical numeric tokens** (`CodeBreaK 100` ≠ `CodeBreaK 200`, `G12C` ≠ `G12D`) | on |
| 4. embedding | cosine ≥ 0.92 and same type → match; 0.85–0.92 → AMBIGUOUS | off (Phase 2) |
| 5. adjudicator | pluggable `Adjudicator(entity, candidates) → id \| None` (e.g. an LLM) | none (Phase 2) |

Outcomes:
- **MATCH_EXISTING** adds a type vote; the majority type wins. It also fills the description if the entity has none; existing descriptions are never overwritten.
- **CREATE_NEW** creates a new entity (uuid4) with the name as its first alias.
- **AMBIGUOUS** creates a *separate* entity flagged `properties.resolution_status = "ambiguous"` and records `ambiguous_with` candidate ids. **It never merges.** List these with `brain entities --ambiguous`.

Two guards keep aliases from causing false merges later:
- **Unsafe aliases are rejected.** An alias that is a token subset or superset of the name (`KRAS` for `KRAS G12C`), or that has different numbers, is not stored.
- **An alias already owned by a different entity is not added.** Both rejections are recorded in the log entry's `details.rejected_aliases`.

## Relationship validation (`resolution/relationship_resolver.py`)

The first failing check rejects the edge. The reason goes to `resolution_log` and to build stats (`relationships_rejected:<reason>`).

1. `unresolved_endpoint`: the source or target local id didn't resolve.
2. Type normalization: snake_case, then the optional `RELATIONSHIP_TYPE_SYNONYMS`, then the optional `RELATIONSHIP_TYPE_INVERSES`, which flips the edge (`targeted_by` → `targets`). `type_not_allowed` applies when an allow-list is set.
3. `self_relationship`: rejected unless `ALLOW_SELF_RELATIONSHIPS`.
4. `missing_evidence`.
5. `evidence_not_in_source`: the quote must match the chunk with `rapidfuzz.partial_ratio ≥ 90` after quote and whitespace normalization. This catches hallucinated or paraphrased "evidence".
6. `source_not_in_chunk` / `target_not_in_chunk`: some surface form of each endpoint must appear in the chunk, as whole words, tolerating a plural suffix. With `EVIDENCE_REQUIRE_ENDPOINTS_IN_QUOTE=true` it must appear in the quote itself; the default is looser so that quotes like "It binds …" are still accepted.
7. `source_substituted_in_evidence` / `target_substituted_in_evidence`: an endpoint that is missing from the quote must not be *replaced* there by another entity of the same type from the same chunk. Example: the quote "AMG 510 selectively inhibits KRAS G12C" cannot support an edge from adagrasib. Plain coreference ("The trial evaluated AMG 510…") names no rival entity, so it passes.

Accepted edges are upserted on `(source, target, type)`:
- Evidence is attached once per chunk.
- `support_count` = the number of distinct supporting chunks; `confidence` = the maximum evidence confidence.
- Evidence rows store the quote, its offsets in the chunk, the grounding score, where each endpoint was found, the raw LLM type, and the extraction run id.

## Retrieval

- **`search_chunks(query, top_k)`**: pgvector cosine search over `chunks.embedding` (HNSW). Each hit includes its document, similarity, and the entities mentioned in that chunk.
- **`GraphQueries`**: `get_entity`, `find_entity` (exact name/alias, then trigram), `get_neighbors`, `get_relationships`, `find_path` (recursive CTE, undirected with direction preserved, cycle check, `max_depth`), `expand_entities` (recursive CTE with `LATERAL` adjacency so both edge indexes are used), and `explain(source, target)`, which returns matching edges with all their evidence.
- **`hybrid_search(query)`**:
  1. Retrieve the top chunks.
  2. Seed entities: those mentioned in the top chunks, plus entities whose name or alias occurs in the query.
  3. Expand the seeds by one hop.
  4. Rank edges touching a seed: edges touching entities named in the query come first, then edges with evidence in the retrieved chunks, then higher support.
  5. Return chunks, entities, relationships and their evidence.

## Consistency evaluation

`scripts/evaluate_consistency.py` uses a separate `<db>_eval` database and repeats the same steps N times:
1. Truncate the graph tables.
2. Run `build(force=True)` with fresh LLM calls.
3. Export a snapshot (`evaluation/snapshot.py`) to `runs/<ts>/run_NN.json`. The snapshot has entities, edges with evidence, decision counts, and the raw LLM output per chunk.

`evaluation/consistency.py` then computes:
- **Entity Jaccard**, two ways: *strict* by canonical name, and *alias-aware* by clustering entities across runs through overlapping name/alias sets.
- **Relationship Jaccard** at three granularities: triple, directed pair, and unordered pair. This separates type drift and direction drift from endpoint drift.
- **Type consistency** (entity and relationship), **direction consistency**, the edge frequency histogram, and lists of stable vs. unstable edges.

`--ontology examples/ontology.json` runs Experiment B: types are constrained through schema enums and the prompt. Results and findings are in [consistency_results.md](consistency_results.md).

## Replay

`chunk_extractions` stores the raw LLM output for each chunk. Running `brain reset --keep-extractions` and then `brain build --replay` re-runs resolution, validation and persistence on *identical* extractions with no LLM calls. This separates the effect of resolver and validator changes from LLM variance.
