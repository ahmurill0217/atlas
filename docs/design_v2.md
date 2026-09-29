# Brain v2 design: metadata-first, extract on demand

**Status:** proposal (2026-09-28).
**Builds on:** the Phase 1 prototype and the experiments in [consistency_results.md](consistency_results.md) and [qa_results.md](qa_results.md).

## 1. Goal

Let people ask an LLM questions over their connected Gmail, Google Drive and Fathom data, and get **accurate, cited answers**:

- "What do I know about <person>?"
- "How did we close <deal/account> before?"
- "Prep me for my call with <person> about <topic>."

### Constraints

- **Scale.** About 1M documents per deployment, growing continuously.
- **Cost and speed.** A full LLM pass over the corpus is ruled out: roughly $0.5–2.3k per pass, and 2–3 weeks at a 200k tokens/min rate limit (§8). It would also have to be re-run on every schema change.
- **Access control is solved by the platform.** Its OAuth integrations and permissions already exist. This system must *preserve* them: every fact stays traceable to source documents, so the platform's permission check can filter at read time. No stored artifact may merge content across documents in a way that escapes that check.
- **GCP.** Postgres + pgvector (Cloud SQL). LLM behind a one-method interface, so OpenAI and Vertex are interchangeable.
- **No CRM.** Accounts are derived from **external email domains**: email headers, and Fathom's `is_external` and `email_domain`. Deals are optional. They can be inferred from cards later, or loaded from a CSV if one exists. If a CRM is connected in future, its ids become extra identifiers (§4).

### Non-goals (for now)

- A complete, open-domain knowledge graph of everything in the corpus.
- A graph database.
- Proactive alerts. Scheduled pre-warming (§5.2) can grow into this later.

## 2. What we learned (and what it rules out)

| Finding | Evidence | Design consequence |
|---|---|---|
| Relationship extraction is unstable run-to-run. It comes from the model, not the pipeline. | Triple Jaccard 0.41 (raw LLM), 0.45 (our pipeline), 0.47 (Cognee) | Don't make LLM-extracted edges the *backbone* of the graph. |
| Entity resolution is the strong part. | Entity Jaccard 0.94; aliases collapse correctly | Keep the resolver. Give it *identifiers* (email, domain, CRM id) as the strongest signal. |
| A fixed schema stabilises extraction, but can make wrong edges consistent. | Triple Jaccard 0.45 → 0.56; stable wrong edges in Experiment B | Use a fixed schema **plus** source/target type rules for each relation. |
| Answers stay accurate even when edges are noisy, because text travels with facts. | QA 0.93–0.98; no answer repeated a known-bad edge | Every fact carries its evidence. Answers cite sources. |
| Graphs help when retrieval must be selective and questions aggregate. They lose value when everything fits in context. | Aggregation 0.79 → 1.00 with graph facts at k=3; parity at k=all | The graph's job is **finding the right documents across time and sources**, not replacing text. |
| Questions about the collection as a whole failed. | MoE and RLHF spot checks: no document nodes; context labelled by file path; retrieval clustered in one document | Documents are graph nodes. Context shows titles and dates. Retrieval caps chunks per document. |
| Full ingestion is fragile and rate-limited at scale. | 78/597 chunks hit 429s; one bad token aborted Cognee's entire build | Incremental, idempotent, per-item failure isolation, Batch API, durable progress. |
| Cheap signals are underused. | Emails, meetings and docs all have participants and timestamps | Build the skeleton from metadata with **zero model calls**. |

## 3. Architecture

```
                 ┌────────────── NIGHTLY / ON SYNC (no LLM) ──────────────┐
 Gmail ─┐        │ normalize → triage → typed Document + metadata          │
 Drive ─┼─► connectors ─┤   (strip quotes/signatures, drop bulk mail,             │
 Fathom ┘        │    dedupe by content hash, keep source timestamps)      │
                 │ ├─► L0 metadata graph: people/orgs/meetings/threads/docs │
                 │ │     identifier-based resolution (email, domain, CRM id)│
                 │ ├─► L1 embeddings (chunks, doc summaries)                │
                 │ └─► L2 entity linking by dictionary (known entities)     │
                 └──────────────────────────────────────────────────────────┘
                 ┌────────────── PRE-WARM (small LLM, Batch API) ──────────┐
                 │ tomorrow's calendar + active deals → cards for their docs│
                 └──────────────────────────────────────────────────────────┘
                 ┌────────────── QUERY TIME ────────────────────────────────┐
 question ─► resolve entities (identifier / alias) ─► seed set:             │
             graph walk (entity → docs, time window)  ∪  vector top-k        │
          ─► cache check ─► extract missing cards (parallel, fixed schema)   │
          ─► resolve + validate ─► assemble context ─► answer with citations │
                 └──────────────────────────────────────────────────────────┘
```

### 3.1 Layers

| Layer | Content | Built | LLM cost |
|---|---|---|---|
| **L0 metadata graph** | Person, Organization, Document (Email / Thread / Meeting / Doc), Deal (if CRM) and their structural edges | on sync, all docs | none |
| **L1 embeddings** | chunk vectors, plus a short summary vector per document | on sync, all docs | embeddings only |
| **L2 linked mentions** | `Document -mentions-> Entity` for *known* entities, found by dictionary match (aliases from L0, CRM, directory); optional local NER (e.g. GLiNER) for unknown names | on sync, all docs | none |
| **L3 content cards** | Fixed-schema extraction per document (decisions, objections, commitments, next steps, deal stage), each field with a quote | **on demand**, plus pre-warm | only for documents actually needed |
| **L4 answer** | retrieval + graph context → cited answer | per question | one call (more if agentic) |

## 4. Data model (changes to the current schema)

What stays: `documents`, `chunks` (pgvector), `entities`, `entity_aliases`, `relationships`, `relationship_evidence`, `entity_mentions`, `resolution_log`, and the extraction cache.

**Additions and changes:**

- **`documents`**: add
  - `doc_type` (email | thread | meeting | doc)
  - `source_system` and `source_id` (Gmail message/thread id, Drive file id, Fathom meeting id)
  - **`source_time`** (sent time, meeting start, Drive `modifiedTime`) — used for ordering and time-window queries
  - `processed_at` — used for incremental sync

  Documents are also **graph nodes**. Relationships can reference a document directly: `Person -attended-> Meeting(doc)`.
- **`entity_identifiers`** (new): `(entity_id, id_type, value)` with `UNIQUE(id_type, value)`.
  - `id_type` is one of: email, domain, directory_user, fathom_recording. The crm_* types are reserved for a future CRM.
  - This is **resolution step 0**. An identifier match is a definitive match. Names become aliases.
- **`relationships`**: add
  - **`origin`**: `metadata` | `linked` | `extracted`
  - `first_seen` / `last_seen`: the min/max `source_time` of its evidence

  Uniqueness stays `(source, target, type)`. Metadata edges are exact; extracted edges keep the validation path.
- **`relationship_evidence`**: `evidence_text` becomes nullable for `origin = metadata`, where the source document *is* the evidence. Add `document_id` so permission filtering is a single join.
- **`cards`** (new): `(document_id, schema_version, card_type, content JSONB, model, created_at)`.
  - This is the extraction cache, keyed by **document content hash + schema version**. It replaces per-question graphs.
  - Card fields are projected into `relationships` (origin = extracted) with quotes as evidence.
- **Permission hook:** all read queries take the caller's allowed document ids (or a platform-provided predicate) and filter evidence through it. An edge is visible only if at least one of its evidence rows is.
  - Aggregates such as `support_count` are computed per request over visible evidence, never cached across users.

## 5. Pipelines

### 5.1 Sync (nightly or incremental; no LLM)

1. **Connectors** pull new and changed items (Gmail history API, Drive changes API, Fathom API). They come in through the platform's existing integrations.
2. **Normalize, per source:**
   - **Email:** strip quoted replies and signatures, so each message contributes only its new text. Group messages into threads.
   - **Docs:** chunk by section; pick up titles.
   - **Fathom** (the `new-meeting-content-ready` webhook). Each payload maps as follows:

     | Payload field | Becomes | LLM? |
     |---|---|---|
     | `title` / `meeting_title`, `scheduled_start_time`, `recording_start_time` | Meeting document node; `source_time` | no |
     | `calendar_invitees[] {name, email, email_domain, is_external}` | Person (identifier = email), Organization (identifier = domain), `attended` edges; external domains = **accounts** | no |
     | `calendar_invitees_domains_type` | external-meeting flag (triage: external meetings are the high-value ones) | no |
     | `recorded_by {name, email, team}` | Person + `recorded` edge; team as a Person attribute | no |
     | `transcript[] {speaker.display_name, speaker.matched_calendar_invitee_email, text, timestamp}` | chunks by speaker turn, **speaker resolved to Person by email**; timestamp kept for evidence | no |
     | `default_summary.markdown_formatted` | card `summary`; document summary vector (no LLM summary needed) | no |
     | `action_items[] {description, assignee.email, completed, recording_timestamp, recording_playback_url}` | **Commitment** nodes: `committed_to` (assignee → commitment) with status and a playback link as evidence | no |
     | `crm_matches` | ignored (no CRM); identifiers if one is ever connected | no |

     Only decisions, objections and deal signals need the LLM (§6). They are extracted from the **summary first** (~500 tokens). The transcript is searched only to attach verbatim quotes, which is about 20× cheaper than extracting from a full transcript.
3. **Triage.** Drop or down-rank:
   - no-reply senders and `List-Unsubscribe` bulk mail;
   - Gmail categories Promotions and Updates;
   - calendar notifications;
   - duplicate attachments (by content hash).

   Triaged-out documents may still get embeddings, but are never card-extracted.
4. **Build L0.** Upsert people (by email), organizations (by domain), documents, and structural edges:
   - Email: `sent`, `received`, `cc`, `in_thread`
   - Meetings: `attended`
   - Docs: `owns`, `edited`, `shared_with`
   - `works_at`, from the email domain
   - Deal links, if a CRM is connected
5. **Build L2.** Build a dictionary from entity aliases and identifiers, plus CRM names. Match whole words in each chunk, with the existing alias guards (no sizes, citations or subset aliases), and write `mentions` edges.
6. **L1 embeddings** for new chunks, plus a document summary vector (see open questions).

This is idempotent: content hashes skip unchanged items, a changed item replaces its chunks, and any edge left without evidence is pruned.

### 5.2 Pre-warm (scheduled, Batch API)

Generate cards ahead of time for:
- documents linked to **tomorrow's calendar events**: the attendees' recent threads and past meetings with them;
- documents linked to **active deals**.

This hides query-time extraction latency for the most common and time-sensitive question: call prep.

### 5.3 Query time

1. **Resolve** entities named in the question: identifier match, then alias match (longest match wins), then fuzzy. Parse any time window ("last quarter", "before").
2. **Seed set** = union of:
   - **graph walk**: resolved entities → their documents via L0/L2 edges, filtered by time window and sorted by `source_time`;
   - **vector top-k** over chunks, with **at most N chunks per document** (fix 2).
3. **Cache check.** For seed documents without a card for the current schema version, extract cards in parallel (small model, fixed schema, split on output overflow). Resolve, validate, persist.
4. **Assemble context:**
   - an **entity brief** for each resolved entity (identifiers, company, role, first/last interaction);
   - a **timeline** of relevant documents with **titles and dates** (fix 1);
   - card facts with quotes;
   - source passages.
5. **Answer** with citations and an explicit "not answerable" field. Later, an agentic loop can call the same functions as tools (`find_entity`, `neighbors`, `timeline`, `evidence`, `search`) for multi-step questions.

## 6. Card schemas (v1)

Every card field that asserts something carries `quote`, which is validated for grounding exactly as today.

**MeetingCard** (Fathom meetings). Most fields come from the Fathom payload, with no LLM.
- From the payload:
  - `summary` ← `default_summary`
  - `attendees[]` ← `calendar_invitees`
  - `organizations[]` ← external invitee domains
  - `commitments[] {text, owner, completed, playback_url}` ← `action_items`
- Extracted by the LLM (from the summary, with quotes located in the transcript):
  - `decisions[] {text, quote}`
  - `objections[] {text, raised_by?, quote}`
  - `deal_stage_signal? {stage, quote}`
  - `sentiment? {value, quote}`

**ThreadCard** (external email threads)
- `summary`
- `participants[]` — from metadata
- `organizations[]`
- `deal?`
- `asks[]`
- `commitments[]`
- `outcome? {text, quote}`
- `deal_stage_signal? {stage, quote}`

**DocCard** (Drive docs)
- `title`
- `doc_kind` (proposal, contract, notes, spec…)
- `organizations[]`
- `deal?`
- `key_points[] {text, quote}`
- `dates[] {what, when, quote}`

### Relation vocabulary and type rules

Cards project into a closed set of relations. Each relation has allowed source and target types, and an edge that violates them is rejected by the validator. This is the fix for Experiment B's "stable but wrong" edges.

| Relation | From → to | Origin |
|---|---|---|
| sent / received / cc | Person → Email | metadata |
| attended | Person → Meeting | metadata |
| owns / edited | Person → Doc | metadata |
| works_at | Person → Organization | metadata (domain), extracted (stated) |
| mentions | Document → Entity | linked |
| discussed | Meeting/Thread → Deal \| Organization \| Topic | extracted |
| raised_objection | Person \| Organization → Deal \| Topic | extracted |
| committed_to | Person → Commitment | extracted |
| decided | Meeting → Decision | extracted |
| stage_of | Deal → Stage (+ `source_time`) | extracted or CRM |

## 7. What we keep from the current code

| Keep as is | Adapt | New |
|---|---|---|
| Postgres + pgvector schema core, Alembic | `documents` (type, source ids, `source_time`) | connectors adapter (platform integrations) |
| Resolver cascade, alias guards, AMBIGUOUS-never-merges | add identifier step 0 | email/transcript normalizers, triage |
| Relationship validator (grounding, substitution, self-loop) | add type-rule check | L0 metadata graph builder |
| Evidence tables, resolution log | evidence `document_id`, nullable quote for metadata | L2 dictionary linker |
| Extraction cache, split-on-overflow, durable per-chunk commits | cache keyed per document + schema version (cards) | card schemas and card extractor |
| SQL traversal (recursive CTEs), hybrid search | context: titles, dates, per-doc cap (fixes 1 and 2) | query-time lazy extraction, pre-warm job |
| `ask` with citations and "not answerable" | entity brief + timeline in context | Batch API extraction mode, token/cost logging |
| QA eval harness, consistency harness | real-use question set | latency and cost metrics per query |

## 8. Cost and latency targets (1M documents)

These are estimates to be replaced with measured token logs. Assumptions: about 1,500 tokens per document after cleanup; `gpt-4o-mini` list price ($0.15/M input, $0.60/M output; Batch API 50% off).

| Item | Estimate |
|---|---|
| Full LLM extraction of everything (**rejected**) | ~$0.5–2.3k per pass, **2–3 weeks** at 200k tokens/min, repeated on every schema change |
| L0 + L2 (no LLM) | compute only; minutes to hours |
| L1 embeddings (text-embedding-3-small) | ~$30 one-time, then incremental |
| Cards, pre-warm (e.g. 2k docs/night) | ~$1–3/night via Batch API |
| Cards, on demand | proportional to *distinct documents asked about*; each extracted once per schema version |
| Query, warm cache | 1 answer call; target p50 < 8 s |
| Query, cold cache | + parallel card extraction; target p50 < 25 s (to be measured) |

## 9. Evaluation

The papers harness carries over, pointed at real data:

- **Question set (40–60, from real use):**
  - person briefs
  - deal histories ("how did we close X", "what objections came up with Y")
  - call prep
  - timeline questions ("what changed since our last call")
  - unanswerable traps

  Reference answers written by someone who knows the accounts.
- **Metrics:**
  - answer score (blind judge)
  - citation validity: cited quotes exist and support the claim
  - abstention on traps
  - **cold vs. warm latency**
  - **$ per query**
  - cache hit rate
  - share of documents ever extracted
- **Arms:**
  - vector-only RAG (baseline)
  - L0 + L2 + vector, no cards
  - full v2

  The middle arm shows how much the zero-LLM layers alone buy.

## 10. Open questions and risks

- ~~Fathom speaker mapping~~ **Resolved:** the payload provides `speaker.matched_calendar_invitee_email` and invitee emails. Unmatched speakers (null email) resolve by name within the meeting's attendees, flagged AMBIGUOUS when unsure.
- ~~CRM~~ **Resolved:** no CRM. Accounts come from external domains, and deals are optional (inferred or CSV). A deal-level question ("how did we close X") is answered at account level unless deals are provided.
- **Test data:** an LLM-generated **synthetic company world** with a hidden record of facts (exact reference answers); the team's **own Fathom recordings** (real payloads); **Enron** email (real headers at scale, for metadata/triage stress tests; non-commercial research licence, internal testing only).
- **External people with no email in the data** (named only in transcripts): they resolve by name within an organization, flagged AMBIGUOUS when unsure.
- **Document summary vectors:** generating a summary costs an LLM call per document. Alternatives are the title plus the first N tokens, or a card summary once one exists.
- **Cache vs. permissions:** cards and edges are per document, so the platform's document-level check applies. Cross-document aggregates are never cached across users.
- **Schema evolution:** a new schema version invalidates cards lazily, on next access, not by bulk re-extraction.

## 11. Phases

| Phase | Scope | Exit criteria |
|---|---|---|
| **A. Skeleton** | Typed documents + `source_time`; L0 builder (Gmail headers, Fathom payload mapping, Drive metadata); identifiers + resolver step 0; L2 linker; fixes 1 and 2 in context assembly. Data: synthetic company world + own Fathom recordings (+ Enron slice for scale). | Person/org/deal timelines correct on spot checks; zero LLM calls in sync; eval arm "L0 + L2 + vector" scored. |
| **B. Cards on demand** | Card schemas + extractor; `cards` cache; query-time lazy extraction; type-rule validation; token/cost logging. | Full-v2 arm beats vector-only on deal-history and call-prep questions; measured cold/warm latency and $/query. |
| **C. Pre-warm + agent** | Calendar- and deal-driven Batch pre-warm; tool-calling query loop for multi-step questions. | Call-prep questions served warm; multi-hop questions improve with no drop in citation validity. |
