# `atlas ask` evaluation: graph-scoped vs plain retrieval (2026-09-29)

> Rounds 1–2 tested graph filtering and boosting of the search. Both were removed after these results; the profile-only and router modes remain. Result files live under `runs/enron/ask/` (not in git).

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

## Round 2: the three fixes (same day)

**Changes:**
- **Conservative linking:** a person needs a full name, alias or email address. A single word never links a person.
- **Boost instead of filter:** `GraphBoostedSearcher` runs each query over the whole index and over the entities' emails, then fuses the two rankings. It's opt-in with `--boost`.
- **Profile-only mode ("facts"):** the metadata profile goes in the prompt and the search is left alone. This is now the default for `atlas ask`.

The baseline answers were reused from round 1. Raw answers are in `runs/enron/ask/results_v2.json`.

| Mode vs baseline | Better | Tie | Worse |
|---|---|---|---|
| Round 1: hard filter + loose linking | 1 | 9 | 5 |
| Round 2: boost + conservative linking | 0 | 12 | 3 (p4, d2, d5) |
| Round 2: profile only | 0 | 15 | 0 |

- **Conservative linking fixed the wrong-person answers:** c5 no longer says "Doug Jacques", and d2 no longer links "Scrimale Bob".
- **The boost still loses emails *about* a person** (p4 Zipper thread) **and one-way first contacts:**
  - The UBS muni-bond proposal came from a salesperson Phillip never replied to, so the broadcast rule left it out of the boosted set.
  - The `painewebber.com` company profile pointed the model at a different contact.
- **Profile only matches plain retrieval on every question.** It also adds correct identity: all addresses, employer, active period and top correspondents.

**Caveat:** run-to-run LLM variance is about the size of these differences. d1 and c5 link nobody, so "profile only" *is* plain retrieval there, yet the two runs cited different emails. Fifteen questions can show large effects, not small ones.

## Conclusion for this corpus

- **Default:** brain retrieval over emails whose headers carry graph-resolved names, plus the graph profile of the people named. The graph earns its place through identity resolution and profiles, not by narrowing search.
- **Not yet tested:** questions only the graph can answer well, where search should struggle. For example: who at Bank of America have we dealt with; when did we last talk to X; who introduced us; everyone on the Bishop's Corner deal. The next question set should cover those. So should a larger corpus, where names in the text stop being enough to find the right emails.

## Round 3: relationship questions

- **Questions:** 10 in `examples/qa/enron_relationship_questions.json`: who at a company, first/last contact, top correspondents, counts each way, "have they ever", external contacts.
- **Expected answers:** computed from the raw email headers (`runs/enron/json_5k`) independently of Atlas, so the graph is not graded against itself.
- **Raw answers:** `runs/enron/ask/relationships.json` and `relationships_v2.json`.

**New graph facts:**
- **Two people:** volume each way, first and last contact, or an explicit "no emails together".
- **A company and a person:** who at that company the person dealt with, with counts and dates.
- **Each person's top contacts outside the organization,** with last contact.
- **Multi-word company names** link to their domain ("Bank of America" → bankofamerica.com).

| Mode (gpt-4o-mini) | Correct | Partly | Wrong |
|---|---|---|---|
| Plain brain retrieval | 2 | 2 | 5 |
| brain + graph facts in the system prompt | 5 | 3 | 1 |
| Graph facts only, no retrieval | 8 | 1 | 0 |

r9 is left out: the expected answer counted co-recipients on a distribution list as Frank Ermis's correspondents, and the graph counts only people he wrote to or heard from. The graph's definition is arguably the right one.

- **Plain retrieval can't count or enumerate.** It sees a sample of emails.
  - "How many did Fraser send Arnold?" → 10 and 0; the right answer is 21 and 113.
  - "Top correspondents" → the people who happened to be retrieved.
  - "People at Dynegy" → 3; the right answer is 1.
- **With the facts in brain's prompt, the model still recounted from retrieved emails for dates and lists** (r6, r7, r8), even when told the facts are complete. The retrieved emails arrive after the system prompt and win.
- **Answering from the graph alone is exact.** Remaining imperfections:
  - **r1:** Steve Lafontaine appears as three "people" (three Bank of America addresses the graph did not link).
  - **r8:** a quote service is listed among contacts.
  - Both are identity/data issues, not answering issues.

## Conclusion across all three rounds

The two systems answer different kinds of question:

| Question kind | Best route |
|---|---|
| What was said (content) | brain retrieval, with the graph profile as context |
| Who / how many / how often / when / who at a company (relationships) | the graph directly |
| Mixed (call prep, deal history) | the graph for people and timeline, brain for content |

Next: a router in `atlas ask` that sends each question (or each part of it) to the right route, then a mixed question set.

## Round 4: the router, mixed questions, and a citation audit

**The router** is in `atlas/retrieval/router.py`.
- **Routing:** a small classification call picks `relationship`, `content` or `mixed`. With no entity recognized, the question goes to `content`.
- **Relationship:** answered from graph facts alone.
- **Content:** answered by brain, with the graph profile as context.
- **Mixed:** both, returned as two labelled sections ("From the relationship graph" / "From the emails"). They are not merged by a model.
- **The asker:** `--as <email>` tells the graph who is asking, so "I / we / you" resolve.

**Citations on both sides:**
- **Graph:** every graph fact carries a source marker `[G#]` naming the email that shows it: first and last contact, each correspondent's latest email, each recent conversation. The graph answer cites these markers.
- **Pair facts are now directional:** "A wrote to B n times, last <date> [G#]" is kept apart from "an email with both on it". An announcement sent to a list containing both people is no longer "the last time A emailed B".
- **brain:** the markers are stripped from the facts brain sees. With them, the model attached `[G#]` to quoted email text they did not belong to.
- **Search citations** are resolved through brain's own citation-number mapping, recorded from each search. Earlier rounds numbered brain's cited documents by position, which mislabelled some sources in the saved results; the answers themselves were unaffected.

**Mixed set:** 8 questions in `examples/qa/enron_mixed_questions.json`; raw answers in `runs/enron/ask/mixed_v5.json`.
- **Routing:** correct for all 8 (7 mixed; the Bishop's Corner question names no known person, so it went to content).
- **Answer quality vs plain retrieval** (graded in `mixed.json` / `mixed_v4.json`):
  - better: m1, m2, m7, m8. The graph section gives exact people, volume and dates; plain retrieval guessed contacts (Grigsby's "top contact" was his fantasy-football partner).
  - tie: m3, m4, m5.
  - m6 was wrong in both before the directional pair facts; it is now correct in the router (last email Allen → Holst 2001-05-07 "California Update 5/4/01").

### Citation audit (`scripts/audit_citations.py`)

- **Graph citations:** checked mechanically (the date in the claim must equal the cited email's date).
- **Search citations:** judged by a model reading the cited email, and every "unsupported" verdict was then read by hand.

| Mixed set | Cited claims | Graph citations verified | Search citations supported | Partly | Unsupported |
|---|---|---|---|---|---|
| Router | 67 | 31 / 31 | 23 | 3 | 10 |
| Plain retrieval | 40 | n/a | 21 | 2 | 17 |

- **Router, 81% verified** (54 of 67 claims). Every graph citation checks out.
  - Most of its unsupported search citations are **true facts attached to the wrong email**: a quote that exists in "RE: wheres the love?" cited to "stuff"; the Prebon trade confirmation cited to the party invite. The model (gpt-4o-mini) wrote the wrong citation number.
- **Plain retrieval, 53% verified** (21 of 40). Its unsupported citations are mostly **generalizations the emails don't contain** ("reflects a collaborative approach…" cited to fantasy-football transaction emails).

### Names as people write them (round 4)

The question sets above name everyone in full. Real questions say "Sarah", "bofa", "Frank".

- **Reading the question:** the classification call now also returns the people and companies named, as written, and spells out well-known abbreviations ("BofA" -> Bank of America).
- **Linking them:**
  - Candidates come by first name, name, address or domain prefix, ranked by how much the asker has emailed each.
  - A clear leader (at least 3x the next) is linked, and the answer says so at the top ("Took "Steve" to mean Steve Lafontaine <...>").
  - Otherwise nothing is answered. The result is route `clarify`, with the candidates, so the caller can ask "which Sarah?".
  - A name nobody has is noted, not guessed.
- **Fallbacks:** a mixed question in which nobody was linked, and a relationship question the facts don't answer, go to search, and the answer says so.

**Casual questions tried on Enron:**

| Question | Result |
|---|---|
| "When did I last email Steve?" (as Arnold) | Steve Lafontaine, cited |
| "Who at bofa have I dealt with?" | bankofamerica.com, via the spelled-out name |
| "What do I know about Mike?" (as Allen) | Mike Grigsby |
| "Prep me for my call with Jennifer" (as Arnold) | Jennifer Fraser |
| "Did Frank ever email the Dynegy people?" (as Allen) | Frank Ermis; "0 times": Dynegy's notices went to him |

**Problems found on the way, now fixed:**
- Senders named just "mike" or "'frank" won an exact-name match and hid the real people.
- "bofa" matched bofasecurities.com by prefix.
- Graph answers lost their citations under structured output.
- "People at a company who dealt with X" had no direction, so the model said Frank emailed Dynegy.

The relationship set re-run in auto mode (`relationships_v3.json`): all 10 answered from the graph with citations. Quality is as before.

### Next

1. **Verify citations at answer time.** Run the audit check live: keep a supported citation; re-point a wrong-number citation to the retrieved email that does support the sentence; flag what nothing supports.
2. **A stronger answer model:** gpt-4o-mini is the weak link in citation numbering.
