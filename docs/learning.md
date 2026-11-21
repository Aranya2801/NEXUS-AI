# Continuous learning and feedback intelligence (Phase 17)

All endpoints are under `/admin`, admin-only, organisation-scoped and audited. Nothing here changes the system automatically: it produces evidence, humans decide.

## 1. Human feedback
`POST /feedback` accepts a thumbs rating, an optional comment, up to 4 **reasons** (`wrong_answer`, `missing_information`, `bad_citation`, `outdated`, `too_long`, `unclear`, `off_topic`, `other`)
and `allow_review` (default **false**). Admins only see a run's question and answer when its author ticked `allow_review`; everyone else's runs stay private (counts and reasons are always aggregated:
`GET /admin/feedback/summary`). `GET /admin/feedback` lists consented feedback with the documents the answer cited. Consent can be withdrawn by re-submitting feedback.

## 2. Evaluation-driven improvement
* `POST /admin/eval/cases` – save a regression case (query + the documents that *should* be retrieved); `POST /admin/eval/cases/from-feedback` promotes a consented thumbs-down run (the admin names the right documents).
* `POST /admin/eval/run {mode, top_k}` runs every active case through the live retriever (as the calling admin) and stores P@5/10, R@5/10, nDCG@5/10 and MRR (same metric code as `docs/evaluation.md`).
* `GET /admin/eval/compare?a=&b=` – paired comparison of two stored runs with bootstrap CIs, plus the cases that got worse/better.
Use it before and after changing embeddings, chunking, retrieval mode or ingestion. **Limits:** retrieval only (not answer quality); a handful of cases is anecdote (the API warns below 5); cases reflect the
queries your team thought of; they are checked against the *current* corpus, so deleting a document silently makes its cases unwinnable.

## 3. Prompt A/B experiments
`POST /admin/experiments` defines variants of **one task's prompt** (each a prompt version from Phase 14 or `builtin`, with weights). Each `/chat` run is assigned by hashing `(experiment, run id)` (independent across runs and
experiments), the assignment is stored on the run, the pinned prompt is what the model receives, and metering records the version. `GET /admin/experiments/{id}` reports per variant: runs, rated runs,
thumbs-down rate with Wilson 95 % CI, abstention and partial-answer rates, mean confidence; pairwise two-proportion tests vs the first variant with Holm adjustment; and a verdict that is
`insufficient_data` until every arm has ≥ 30 rated runs. `POST …/conclude {winner}` is a **human** decision: it activates the winner's prompt (rollback stays one call away). Only one running experiment per task.
**Limits:** chat runs only (not reports/agentic runs); thumbs are self-selected; runs are treated as independent although users contribute several; fixed-sample tests – checking repeatedly and stopping at the first
p < 0.05 inflates false positives; no sequential testing or multiple-metric correction beyond Holm over the two reported metrics; a prompt that wins on thumbs may still hurt faithfulness – check the verification stats and run Phase 11-14 evaluations.

## 4. Automated quality monitoring
`GET /admin/quality` summarises a recent window against a preceding baseline (default 14 + 14 days) from signals recorded for every run: failures, human-review requests, abstentions, partial answers, mean retrieval
confidence, unsupported claims removed, thumbs-down rate; daily series included. Alerts need ≥ 30 observations per window, p < 0.01 and a practically relevant change (≥ 5 points / 0.05); improvements are labelled as such.
A background monitor (`NEXUS_QUALITY_MONITOR_INTERVAL_SECONDS`, default hourly, 0 = off; one replica at a time via lease) writes a `quality.alert` audit row per org and metric (at most once per 24 h) and sets the
`nexus_quality_regressions_active` gauge. **These are proxies**: they show behaviour changed, not why, and a change in *user mix* looks the same as a change in *system quality*. Thresholds are uncalibrated defaults.
No notification channel (email/Slack) is wired: alerts live in the audit log, `/admin/quality` and the gauge.
