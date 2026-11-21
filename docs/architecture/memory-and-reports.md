# Memory and research reports (Phase 7)

## Memory: five kinds, one owner
| Kind | Storage | Lifetime | Used for |
|---|---|---|---|
| Short-term | `messages` rows of the conversation | until the conversation is deleted | last 6 turns feed the follow-up rewrite |
| Long-term | `memory_items` (`long_term`), explicit key/value set by the user | no expiry | style preferences; passed to the answer prompt as a `PREFERENCES` block that may affect wording only |
| Session | `memory_items` (`session`, scope = conversation id) | 24 h TTL | working set: recent queries, cited documents, entities |
| Research context | `memory_items` (`research_context`) | until deleted | named workspace: pinned documents + page-cited findings; **scopes chat retrieval to its documents** |
| Document interaction | `memory_items` (`doc_interaction`) | 90 d TTL, rolling cap of 500 | recently cited/viewed documents |

Guarantees (tested on SQLite and PostgreSQL):
* Every query is filtered by `(org_id, user_id)`. **Admins cannot read other users' memory.** Foreign ids return 404.
* **Expiry:** expired items are hidden at read time immediately and physically deleted by a background purge task.
* **User control:** `GET /memory` shows everything stored; delete one item, one kind, or everything; `include_conversations=true`
  also erases conversations, runs and the LangGraph checkpoints (which contain questions, evidence and answers).
* **Delivered answers only:** session and document memory are written when an answer is completed, not while it awaits human review.
* **Access is re-validated on read.** Saved findings and document history are checked against the caller's *current* access; if a
  collection became restricted or a document was deleted, the snapshot is hidden and the quote is never returned.
* Values are size-limited (8 KB), keys validated, per-kind limits enforced (document history evicts the oldest instead of failing).
* **Do not store secrets in memory**: values are plain JSON in the application database.

## Research reports
`POST /reports` (async, status + progress polling) → `GET /reports/{id}` → `GET /reports/{id}/export?format=md|docx|pdf`.

Pipeline (`app/reporting/builder.py`): discover documents (hybrid retrieval, optional graph; a document counts only if a retrieved
passage covers ≥ 50% of the topic's stemmed terms) → per section scoped retrieval + rerank → sanitised passages → grounded claims →
the same fact-verification as chat (unsupported claims removed, weak ones flagged) → sections built from **verbatim** sentences:

| Section | Source |
|---|---|
| Methodologies, Datasets, Limitations | verified claims, each with citation(s) |
| Reported quantitative results | table of verbatim result sentences + numbers + page |
| Possible contradictions | numeric/polarity **candidates** from the analytical agent, flagged heuristic |
| Research gaps | sentences that state open problems/future work themselves; NEXUS infers none |
| Executive summary | first verified item per major section (no new text) |

**Never invent.** Final guard: every number in a cited block must occur in the chunks it cites, otherwise the block is dropped and
recorded. A property test checks against the database that every quote is verbatim from its chunk, every page matches, and every
number is sourced. Hostile model output (invented numbers, uncited claims) is rejected; with the LLM down, generation falls back
to extractive claims.

**Redaction on read and export:** if the reader's access to a cited source has since been revoked (or the document deleted), every
claim/row citing it is replaced by a notice, citations show `[restricted source]`, and the same applies to JSON, Markdown, DOCX
and PDF. Reports are visible to their owner and org admins only.

**Exports** all render the same structure with `[n]` markers and a Sources list (title, file, page, section, version, verbatim
quote). Round-trip tests re-open the DOCX (headings, table) and PDF (text, page footers). PDF uses DejaVu Sans if found (or
`NEXUS_PDF_FONT_PATH`) for Unicode; otherwise Helvetica, replacing non-Latin-1 characters with `?`.

A sample (synthetic documents, offline mock) is in `docs/examples/` and `datasets/sample_documents/`.

## Limitations
* **Heuristics, not calibrated:** document relevance floor (0.5 topic-term coverage), contradiction detection (it flags
  "Alpha reaches 18.3" vs "Beta reaches 21.9" as a candidate even though they are different models), gap cues, section cue
  words used to focus sentences in mock mode. Lexical relevance will miss semantically relevant documents that share no topic terms.
* Results from different datasets/metrics are listed side by side, never normalised or ranked.
* Reports built with the offline mock only extract sentences; they demonstrate structure and verification, not model quality.
  The mock warning is embedded in every such report.
* Reports are not versioned: regenerate after uploading new documents. A restart marks in-flight reports `failed (interrupted)`.
* No chart generation yet; PDF layout is simple (no table-of-contents or hyperlinks).
