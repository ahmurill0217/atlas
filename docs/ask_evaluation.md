# `atlas ask` evaluation: graph-scoped vs plain retrieval (2026-09-29)

## Setup

- **Corpus:** 5,000 Enron emails from the Allen, Arnold, Cuilla and Ermis mailboxes.
  - Atlas graph built from metadata only.
  - Every email indexed in brain (OpenSearch hybrid search, nomic embeddings), 10,315 chunks.
- **Questions:** 15 in `examples/qa/enron_questions.json`: 5 person profiles, 5 deal histories, 5 call preps.
- **Two modes per question:**
  - **graph:** Atlas links names and scopes the search to those entities' emails, then adds a metadata profile to the prompt.
  - **baseline:** brain alone over all 5,000 emails.
  - The baseline also benefits from Atlas indirectly: every indexed email starts with a From/To header that uses graph-resolved names.
- **Model:** gpt-4o-mini, `force_search`.
- **Raw answers:** `runs/enron/ask/results.json`.
- **Grading:** by hand, against the cited emails and each question's key points.

## Result

| | Graph better | Tie | Baseline better |
|---|---|---|---|
| Person (p1–p5) | 0 | 4 | 1 (p4) |
| Deal history (d1–d5) | 0 | 2 | 3 (d2, d4, d5) |
| Call prep (c1–c5) | 1 (c3) | 3 | 1 (c5) |
| **Total** | **1** | **9** | **5** |

**Graph scoping, as built, made answers worse more often than better.** The baseline was already good because names appear in the text and headers, so plain search finds a person's emails.

## Why the graph lost

1. **Hard scoping drops emails *about* someone** (p4, d4). The scope keeps only emails the entity is on.
   - The key Andy Zipper thread ("whats the deal with andy zipper … reporting to me") is between Jennifer Fraser and John Arnold. Zipper isn't a participant, so it was filtered out, and the baseline found it.
   - The "trading with Campbell" plan A / plan B email is internal Enron mail about Campbell, with nobody from campbell.com on it.
2. **Single-word name linking is unreliable** (d2, c5, d5). Each wrong link either scoped the search to the wrong emails or put facts about the wrong person in front of the model. The baseline answered all three correctly.
   - **"Bob"** matched "Scrimale Bob", a name read from `scrimale.bob@…`, which is actually surname.firstname.
   - **"Jacques"** matched a "Doug Jacques", while the Jacques in question is `jacquestc@aol.com`. The answer then called him "Doug Jacques".
   - **"PaineWebber"** matched `painewebber.com` (a stock-options email), but the muni-bond proposal came from `ubspw.com`.

## Where the graph helped

- **Profiles:** addresses across aliases, employer, active period and frequent correspondents came out correct and well structured (p1, p3, p5, c2).
- **c3 (Mike Maggi prep):** the scoped search surfaced his real work topics (Options Advisory Committee, gas floor), while the baseline answer was generic.
- **No invented facts:** the metadata profile is accurate wherever the linking was right.

## What to change

1. **Use the graph to boost, not to filter.** Run brain unscoped, and either run a second scoped search or re-rank so emails involving the entities come first. Documents about an entity can then still be found.
2. **Link conservatively.** Link on full names and email addresses. A single word links only when it's unambiguous across both first and last names. An uncertain link means no scope and no facts.
3. **Add facts only when the link is confident,** and label them "from email metadata".

## Other observations

- **The metadata-only graph plus text search answered most questions well in both modes.** For this corpus size, "metadata graph + vectors" looks sufficient for who / what-was-said questions. The graph's value is identity (one person across addresses, names in headers) and profiles, more than narrowing the search.
- **Infrastructure:** indexing ran at about 3 emails/s on CPU (Docker on Mac), and OpenSearch crash-looped under memory pressure until the Onyx stack was stopped. brain retried failed documents correctly.
