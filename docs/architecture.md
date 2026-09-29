# Atlas architecture (as built)

## What it is for

This is a brain over a company's Gmail, Drive and Fathom meetings. It answers three kinds of question, always with cited sources:

| Kind | Example | Answered by |
|---|---|---|
| Relationship | "Who at Acme have we dealt with?", "When did I last email Sarah?" | the graph |
| Content | "What did they say about pricing?" | brain (retrieval) |
| Mixed | "Prep me for a call with Sarah", "How did the Acme deal go?" | both, in two labelled sections |

## Two systems, one document id

```
source (email / doc / meeting JSON, PDF, DOCX)
   │  atlas/ingestion: adapter -> NormalizedDocument -> segmentation
   ▼
Atlas (Postgres, schema kg)                         brain (../brain; OpenSearch + local embeddings)
  structured extractor: metadata -> candidates        atlas index: header (From/To/Date/Subject, graph
  compiler: ontology checks, identity resolution,       names) + own text -> chunks -> embeddings
    evidence, review queue, audit log
   │                                                   │
   └──────────── shared key: document_id ──────────────┘
```

- **Atlas** builds the graph from metadata only (who wrote to whom, when; meeting invitees, speakers, action items; document authors). It uses no model at ingest.
  - Every fact has evidence.
  - The ontology (`ontology/v1_3`) is the only schema.
  - Ambiguity goes to a review queue, not a guess.
  - Identity: one person across addresses (`mailbox` identifier, address patterns); organizations by registrable domain; broadcast senders recognized.
- **brain** is an Onyx-derived library: chunking, local embeddings, hybrid search and cited answers. Atlas sends it each document's own text, prefixed with a header that uses graph-resolved names.

## Answering (`atlas ask`, `atlas/retrieval/`)

1. **`ask.py`** links the people and companies the question names.
   - Conservative: full names, aliases and emails for people; domain labels for companies.
   - The asker (`--as`) resolves I / we / you.
2. **`ask.py`** builds facts about them from the graph:
   - profiles and correspondents;
   - pair facts (who wrote to whom, when);
   - who at a company someone dealt with.

   Each fact carries a `[G#]` marker naming an email that shows it.
3. **`router.py`** classifies the question (one small model call) and answers:
   - **relationship:** from the facts alone; cites `[G#]`.
   - **content:** brain answers with the profile as context; cites the emails it retrieved.
   - **mixed:** both. The two parts are not merged by a model, because a model overwrites the graph's exact numbers with guesses from the retrieved sample.

## What the evaluation showed (`docs/ask_evaluation.md`, Enron, 5,000 emails)

- **Content questions:** brain alone and brain + profile tie.
  - Narrowing or boosting search to the graph's documents made answers worse (it hid emails *about* someone). That mode was removed.
- **Relationship questions:** answering from the graph is exact (8 correct, 1 partial of 9 scored). Plain retrieval got 2 right, 2 partial, 5 wrong: it counts from a sample.
- **Mixed questions:** the router is better or tied on all 8.
- **Citations:** 81% of the router's cited claims are backed by the cited email (every graph citation is), vs 53% for plain retrieval. Most of the router's misses are true claims with the wrong citation number.

## Not built yet

1. **Citation checking at answer time:** re-point or flag a citation the email doesn't support. Target: at least 95% verified.
2. **Deals:** "how did we close this deal" needs a deal reconstructed from the threads, meetings and docs involving a customer's people. The graph has no deal entity yet.
3. **Linking mentions inside document text** to known people and companies. Documents are thin in the graph until then.
4. **Real data:** everything above is Enron. The next gate is a pilot on real Gmail, Drive and Fathom with real questions.
5. **Production:** embedding capacity (GPU or hosted), incremental sync, delete and permission propagation across both stores.

## Layout

| Path | What |
|---|---|
| `atlas/ingestion` | adapters (email, Fathom meeting, generic JSON, PDF, DOCX, text), `NormalizedDocument`, segmentation |
| `atlas/extraction/structured.py` | metadata → candidate entities and edges |
| `atlas/compiler`, `atlas/resolution`, `atlas/ontology` | the only path into the graph: validation, identity, policy |
| `atlas/graph` | repository (writer), queries, `explain_edge`, HTML viewer |
| `atlas/review`, `atlas/provenance` | review queue, audit log, evidence |
| `atlas/retrieval` | `brain_bridge` (indexing), `ask` (graph facts), `router` (answering) |
| `ontology/v1_0 … v1_3` | versioned ontology; `CHANGELOG.md` |
| `migrations` | Alembic; schema `kg` |
| `scripts` | Enron converter, sample generator, ask evaluator, citation audit |
| `examples` | business samples; Enron question sets |
