# Cognee `cognify` pipeline: analysis

**Reference:** `references/cognee/`, a shallow clone of https://github.com/topoteretes/cognee. The commit is `c4cd8ceb9509dff6bddfabdadbeab7cc040bc32b` (2026-09-27), and Cognee is Apache-2.0 licensed.

**Purpose:** the goal was to understand how Cognee turns documents into a knowledge graph, then decide what to reimplement in our small Postgres-only service (`brain/`). No Cognee code is imported or copied. The ideas we borrow are listed in [§4](#4-what-we-reuse-concepts-not-code).

File paths below are relative to `references/cognee/`, and `file:N` means a line number. Several paths suggested in the original brief have moved or been renamed. The brief pointed at `extract_graph_from_data.py`, but the default path now enters through `extract_graph_and_summarize`; §1 lists the current paths.

---

## 1. End-to-end flow

```
cognee.add(data)                                   cognee/api/v1/add/add.py:39
  └─ resolve_data_directories → ingest_data        cognee/tasks/ingestion/ingest_data.py:116
        hash (MD5) → dedup lookup → loader → Data row (pipeline_status={})

cognee.cognify()                                   cognee/api/v1/cognify/cognify.py:110
  ├─ get_default_tasks()                           cognify.py:564-641
  ├─ per-item routing (standard/code/dlt)          cognee/modules/cognify/routing.py:23-41
  └─ run_pipeline("cognify_pipeline")              cognee/modules/pipelines/operations/pipeline.py:53
        1 classify_documents                       cognee/tasks/documents/classify_documents.py:138
        2 extract_chunks_from_documents            cognee/tasks/documents/extract_chunks_from_documents.py:32
             └─ TextChunker → chunk_by_paragraph → chunk_by_sentence → chunk_by_word
        3 extract_graph_and_summarize              cognee/tasks/graph/extract_graph_and_summarize.py:13
             ├─ extract_graph_from_data            cognee/tasks/graph/extract_graph_from_data.py:198
             │    ├─ extract_content_graph (1 LLM call / chunk)
             │    │     cognee/infrastructure/llm/extraction/knowledge_graph/extract_content_graph.py:18
             │    └─ integrate_chunk_graphs        extract_graph_from_data.py:87
             │          ├─ construct_data_points_and_edges   cognee/modules/graph/utils/expand_with_nodes_and_edges.py:218
             │          ├─ find_existing_edge_identities     cognee/modules/graph/utils/retrieve_existing_edges.py:11
             │          └─ attach_new_edges_to_data_points   expand_with_nodes_and_edges.py:245
             └─ summarize_text (1 LLM call / chunk) cognee/tasks/summarization/summarize_text.py:16
        4 add_data_points                          cognee/tasks/storage/add_data_points.py:70
             ├─ get_graph_from_model → deduplicate_nodes_and_edges
             ├─ graph_engine.add_nodes → index_data_points (embeddings)
             ├─ graph_engine.add_edges → index_graph_edges
             └─ capture edge evidence rows
        5 record_provenance        (opt-in, PROVENANCE_TRACKING, default off)
        6 detect_contradictions    (opt-in, CONTRADICTION_DETECTION, default off)
        7 resolve_temporal_contradictions (only if functional_relationships given)
```

### Default task list

| # | Task | Batch size | LLM |
|---|---|---|---|
| 1 | `classify_documents` | 1 | no |
| 2 | `extract_chunks_from_documents(max_chunk_size, TextChunker)` | 1 | no |
| 3 | `extract_graph_and_summarize(graph_model=KnowledgeGraph, …)` | `chunks_per_batch` (2000) | yes |
| 4 | `add_data_points` | `chunks_per_batch` | no |
| 5-7 | provenance / contradictions / temporal (opt-in) | `chunks_per_batch` | 6 only |

**How batching and concurrency work** (`cognee/modules/pipelines/tasks/task.py:188-341`, `operations/run_tasks_base.py:262-283`):
- Each task's `batch_size` batches the output of the task before it.
- Each Data item runs the whole chain on its own.
- Up to `data_per_batch=20` items run concurrently.

---

## 2. Stage by stage

### 2.1 Ingestion (`cognee.add`)

- **`ingest_data`** (`cognee/tasks/ingestion/ingest_data.py:116-556`) runs in four steps:
  1. Save each item and take an **MD5 content hash**: `get_file_content_hash.py:18-46`, `TextData.py:46-48`.
  2. Look up an existing row with `identify_many` (`cognee/modules/ingestion/identify_many.py:16-73`). The lookup is scoped by `(dataset, content_hash, owner, tenant)`.
  3. Run the loader engine (`cognee/infrastructure/loaders/LoaderEngine.py:32-49`: text, pypdf, unstructured, docling, …) to produce a text file.
  4. Upsert the `Data` row (`cognee/modules/data/models/Data.py`).
- **Changed content resets `pipeline_status`**, so cognify reprocesses that document (`ingest_data.py:475-476`). `refuse_changed_existing_documents` stops `add()` from silently replacing a changed file and points the caller to `update()`.
- **Data ids are random `uuid4`.** Dedup is a lookup, not a content-derived identity.

### 2.2 Classification

`classify_documents` (`classify_documents.py:138-182`) maps the file extension to a `Document` subclass. `TextDocument` is the fallback. The Document node's id equals the Data row id, and node-set tags come from `external_metadata`.

### 2.3 Chunking

- `extract_chunks_from_documents` (`extract_chunks_from_documents.py:32-61`) streams `document.read(max_chunk_size, TextChunker)`.
- **`TextChunker`** (`cognee/modules/chunking/TextChunker.py:13-114`) packs paragraph pieces greedily up to `max_chunk_size`.
  - The pieces come from `cognee/tasks/chunks/chunk_by_paragraph.py`, which builds on `chunk_by_sentence.py` and `chunk_by_word.py`.
  - The token budget is measured with the embedding tokenizer.
  - **There is no overlap.** Overlap exists only in `TextChunkerWithOverlap` and `LangchainChunker`.
- **Chunk id** (`cognee/modules/chunking/chunk_id.py:15-22`) is `uuid5(f"{document_id}:{sha256(text)}:{occurrence}")`, which is content-derived and stable.
- **Default chunk size** is `min(embedding_max_tokens, llm_max_tokens // 2)` (`cognee/infrastructure/llm/utils.py:14-56`), about **8191 tokens**. That is very large for extraction.
- `DocumentChunk` (`cognee/modules/chunking/models/DocumentChunk.py`) has two parts:
  - Graph fields: `is_part_of` (Document) and `contains` (Entities).
  - Private lists used for provenance: `_produced_edge_identities` and `_provenance_edges`.

### 2.4 LLM extraction

- **`extract_content_graph`** (`knowledge_graph/extract_content_graph.py:18-50`) sends the system prompt `generate_graph_prompt.txt` and the chunk text as the user message, and asks for `response_model=KnowledgeGraph`. It makes one call per chunk, and all calls in a batch run concurrently (`extract_graph_from_data.py:224-232`).
- **Structured output** goes through `LLMGateway.acreate_structured_output` (`cognee/infrastructure/llm/LLMGateway.py:113-160`).
  - The default framework is `litellm_native`: `litellm.acompletion(response_format=PydanticModel)` (`structured_output_framework/litellm_native/native_adapter.py:268-298`).
  - It falls back from strict to non-strict JSON schema, and then to JSON-object mode with up to 3 validation retries.
  - `instructor` and BAML are available as alternatives.
- **The IR models** are in `cognee/shared/data_models.py:47-77`:
  - `Node{id, name, type, description}`
  - `Edge{source_node_id, target_node_id, relationship_name, description}`
  - `KnowledgeGraph{nodes, edges}`

  The LLM's node ids are chunk-local wiring keys and are never persisted.
- **The prompt** asks for basic, general node types ("Person", not "Mathematician"), human-readable ids, snake_case relationship names, and the "most complete identifier" for coreference.
- There is also a multi-round cascade extractor in `extract_graph_from_data_v2.py` (nodes → relationship names → triplets). **It is not used by default.**
- Summarization (`summarize_text`) runs in parallel and makes a second LLM call per chunk.

### 2.5 Graph construction

`construct_data_points_and_edges` (`expand_with_nodes_and_edges.py:218-242`) runs in three parts.

**Nodes** (`_convert_extracted_nodes_to_data_points`, `:137-166`):
- **Entity id** is `Entity.id_for(name)`, which expands to `uuid5("Entity:" + name.lower().replace(" ", "_").replace("'", ""))` (`cognee/infrastructure/engine/models/DataPoint.py:163-193`). Identity is the lightly normalized surface string.
- If two nodes in one chunk share a name, they get **chunk-scoped ids** (`:92-110`), and those never merge with anything else.
- **EntityType** nodes are created with `EntityType.id_for(type)` and linked by `is_a`.
- **Chunk → entity** is recorded as a `contains` edge with `edge_text` "Document chunk mentions X: …" (`:115-134`).

**Edges** (`_add_extracted_edges`, `:169-215`):
- Edges whose endpoint is an unknown local id are **silently dropped**.
- The rest are keyed by `EdgeIdentity(source_id, target_id, relationship_name)`, and within a batch the first description wins (`setdefault`, `:209`).

**Existing edges** (`find_existing_edge_identities` / `attach_new_edges_to_data_points`):
- Edges already in the graph DB are **not re-written**; they only get a source reference.
- So a later mention never updates an edge's text or strength.

**Graph walk** (`get_graph_from_model`, `cognee/modules/graph/utils/get_graph_from_model.py:156-237`):
- Turns the Pydantic object graph into flat node and edge lists. Any field holding a DataPoint or an `(Edge, DataPoint)` becomes an edge.
- Resulting shape: `TextSummary -made_from-> DocumentChunk -is_part_of-> Document`, `DocumentChunk -contains-> Entity -is_a-> EntityType`, `Entity -<rel>-> Entity`.

**Ontology** (off by default): `construct_data_points_and_edges_with_ontology` canonicalizes names and types against an OWL ontology using `difflib` matching with cutoff 0.8 (`cognee/modules/ontology/matching_strategies.py:24-51`).

### 2.6 Deduplication

It is entirely deterministic and ID-based:
- In-batch: `deduplicate_nodes_and_edges.py:4-20`, first one wins.
- Across chunks and documents: the same `uuid5(name)` means the same entity.
- Edges: exact `(src, tgt, rel)` triple.

**What is missing:**
- No alias table.
- No fuzzy or embedding resolution, apart from the optional ontology matching.
- No Unicode or punctuation normalization.
- Entity type is not part of identity.
- Relationship names get only case/space normalization, and direction is whatever the LLM chose.

### 2.7 Provenance

- **Stamping:** `source_pipeline` / `source_task` / … fields are set on every DataPoint (`run_tasks_base.py:33-118`).
- **Source-ref keys** on graph rows (`cognee/infrastructure/databases/provenance/source_refs.py`, `cognee/tasks/storage/chunk_ownership.py`) record which data item or chunk produced each node and edge. They are used for deletion and updates.
- **`provenance_edge_evidence` table** (`cognee/modules/provenance/edge_evidence/models.py:18-53`, on by default): one row per (chunk, edge occurrence), with the chunk id, edge id, relationship name, run id and confidence. **It stores no evidence text or span** (`buffer.py:138-165`), so the supporting sentence is not recorded.
- **Hash-chained audit ledger** (`record_provenance`): opt-in.

### 2.8 Persistence

- **`add_data_points`** (`add_data_points.py:70-422`) writes graph nodes, then embeds the `index_fields` of each DataPoint type, then writes edges and indexes the edge text.
- **Vector collections** are one per `Type_field`: `DocumentChunk_text`, `Entity_name`, `EntityType_name`, `TextSummary_text`, `EdgeType_relationship_name`. Entities are embedded by **name only**.
- **The PGVector adapter** (`cognee/infrastructure/databases/vector/pgvector/PGVectorAdapter.py`) keeps one table per collection. It never updates a vector on conflict and creates **no ANN index**.
- **The Postgres graph adapter** (`cognee/infrastructure/databases/graph/postgres_demo/`) is labelled a demo. It has two tables:
  - `graph_node(id, name, type, properties JSONB, …)`
  - `graph_edge(PRIMARY KEY (source_id, target_id, relationship_name), properties JSONB, …)`

  Node upserts replace the whole `properties` blob, so the last writer wins on descriptions.
- **The default stores** are Kuzu/Neo4j for the graph, LanceDB for vectors and SQLite for relational data. Postgres + pgvector is optional.

### 2.9 Base models

- **`DataPoint`** (`cognee/infrastructure/engine/models/DataPoint.py:28-387`) is a Pydantic base with:
  - `id`, set from `identity_fields` through `id_for`
  - `metadata.index_fields`, which says what gets embedded
  - `belongs_to_set`, `ontology_valid`, `version` (never bumped by cognify), `importance_weight` / `feedback_weight`
  - provenance fields
- **`Edge`** (`cognee/infrastructure/engine/models/Edge.py:13-164`) is a generic typed edge with `relationship_type`, optional `weight`/`weights`, `properties` and `edge_text`. Extraction sets no weight or confidence.

### 2.10 Runner and incremental processing

- `run_pipeline` (`cognee/modules/pipelines/operations/pipeline.py:53`) calls `run_tasks`, which calls `run_tasks_data_item_incremental` (`operations/run_tasks_data_item.py:110-282`).
- A document is skipped **as a whole** if `Data.pipeline_status[pipeline][dataset]` is completed. That status is reset only when the content hash changes.

---

## 3. Weaknesses observed (relevant to our goals)

1. **Identity is the surface string.** "Sotorasib", "AMG 510" and "Lumakras" become three entities unless the LLM happens to pick the same name. "AMG510" vs "AMG 510" also split. There are no aliases.
2. **Edges don't accumulate support.** Existing edges are skipped, the first `edge_text` wins, and there is no support count or confidence aggregation.
3. **Evidence has no text.** You can find *which chunk* produced an edge, but not *which sentence* supports it, and nothing checks that the edge is actually supported by that chunk.
4. **Edge endpoints referencing unknown local ids are dropped silently.** Duplicate local ids keep the first node, with only a warning.
5. **Relationship vocabulary fragments.** Relationship names are free-form and direction is not normalized.
6. **Chunks of about 8k tokens with one call each.** Recall and stability rest on a single large-context extraction.
7. **Nondeterminism is invisible.** Nothing measures how much the graph changes between runs.

These are the points our design targets.

---

## 4. What we reuse (concepts, not code)

| Cognee idea | Where in Cognee | Where in `brain/` |
|---|---|---|
| Pipeline = ordered stages: add → chunk → extract → integrate → persist | `get_default_tasks` | `brain/pipeline/ingest.py`, `brain/pipeline/build_brain.py` (plain functions, no Task runner) |
| Content hash for idempotent ingestion; changed content means reprocess | `ingest_data`, `pipeline_status` reset | `documents.content_hash`; `ingest()` skips unchanged docs and replaces the chunks of changed ones |
| Paragraph → sentence packing chunker, no cut mid-sentence, no overlap by default | `TextChunker`, `chunk_by_*` | `brain/chunking/chunker.py` (one function, keeps character offsets) |
| Content-derived, deterministic chunk ids | `chunk_id.py` | `uuid5(document_id:index:sha256)` in `brain/pipeline/ingest.py` |
| One structured-output LLM call per chunk into a Pydantic IR (`KnowledgeGraph`) | `extract_content_graph`, `data_models.py` | `brain/extraction/` (`ExtractedKnowledgeGraph`, OpenAI structured outputs) |
| LLM ids are chunk-local wiring keys, never global ids | `_calculate_entity_ids_by_extracted_node_id` | `local_id`, mapped to canonical ids by the resolver |
| Prompt rules: general reusable types, snake_case relations, most complete identifier | `generate_graph_prompt.txt` | `brain/extraction/prompts.py` (own wording, plus aliases and verbatim evidence) |
| Edge identity = (source, target, relationship type) | `EdgeIdentity`, `graph_edge` PK | `UNIQUE (source_entity_id, target_entity_id, relationship_type)` |
| Chunk → entity "contains" links as mention provenance | `DocumentChunk.contains` | `entity_mentions` table (with offsets) |
| Per-(chunk, edge) evidence rows with run id and confidence | `provenance_edge_evidence` | `relationship_evidence`, which **adds evidence_text** and validation details |
| Optional ontology to constrain types | ontology resolver | `ALLOWED_ENTITY_TYPES` / `ALLOWED_RELATIONSHIP_TYPES`, enforced as schema enums (Experiment B) |
| Postgres graph as node and edge tables | `postgres_demo/tables.py` | `entities` / `relationships` with typed columns instead of a JSONB blob |
| pgvector for chunk embeddings | `PGVectorAdapter` | `chunks.embedding VECTOR(1536)` **with an HNSW index** |

### Where we deliberately differ

- **Identity is decided by a resolver, not a hash.** The resolver checks, in order: exact normalized name, alias, fuzzy (with numeric-token guards), then embedding, which is optional. Each check returns MATCH / CREATE / AMBIGUOUS, and every decision is logged in `resolution_log`.
- **Aliases are first-class** (`entity_aliases`).
- **Relationships are validated before persisting.** The validator checks that endpoints resolved, the type is normalized and allowed, it is not a self-loop, evidence is present, the evidence is grounded in the chunk text, and both endpoints are named in the chunk.
- **Repeat mentions strengthen an edge.** They attach evidence and bump `support_count` instead of being dropped.
- **Descriptions are not overwritten.** The first non-null description is kept. Entity type is decided by a majority vote across extractions.
- **The raw LLM output is stored per chunk** (`chunk_extractions`), which gives a cache and a debugging trail.
- **A consistency evaluation measures nondeterminism explicitly** (`scripts/evaluate_consistency.py`).

---

## 5. What we intentionally do NOT reproduce

- The generic `Task` / `run_pipeline` runner, async generator batching, `data_per_batch` semaphores and pipeline-run bookkeeping. Plain functions and a thread pool are enough.
- Datasets, users, tenants, ACLs, node sets, `belongs_to_set`, `importance_weight` and `feedback_weight`.
- Pluggable graph and vector backends (Kuzu, Neo4j, FalkorDB, LanceDB, Qdrant, …) and the unified engine. **We use Postgres + pgvector only.**
- The multi-format loader engine (PDF, audio, image, docling, unstructured, DLT, code graphs). Phase 1 handles TXT and Markdown.
- `DataPoint`-reflection graph building (`get_graph_from_model`), `EntityType` nodes and `is_a` edges. We store types as a column.
- Summaries (`summarize_text` / `TextSummary`), triplet embeddings and `EdgeType` vectors.
- Contradiction detection, temporal supersession, the cascade extractor, the GLiNER extractor, the hash-chained provenance ledger, and search types / graph-completion RAG.
- litellm, instructor and BAML. We use one `StructuredLLM.generate(system, user, schema)` protocol with an OpenAI implementation.
- OWL ontology parsing. Experiment B uses flat allow-lists.

---

## 6. Proposed minimal architecture (what we built)

```
ingest (offline, no LLM)                 build (LLM + embeddings)
─────────────────────────                ──────────────────────────────────────────────
discover_files → parse → chunk           embed chunks lacking embeddings (pgvector)
  → documents, chunks rows               extract (thread pool, 1 structured call / chunk)
                                           → clean_extraction (intra-chunk dedup, dangling ids)
                                         per chunk, in deterministic order, one transaction:
                                           EntityResolver  → canonical entity ids (+ aliases, mentions, log)
                                           RelationshipValidator → accept/reject (+ log)
                                           upsert edge (unique triple) + attach evidence + support_count
                                           store raw extraction (cache)
                                         prune edges without evidence / entities without mentions
```

Read side: SQL graph traversal (joins and recursive CTEs), pgvector chunk search, and a hybrid search that returns chunks, entities, edges and evidence. See `docs/architecture.md` for details.
