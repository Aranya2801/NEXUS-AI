# Adaptive Retrieval: pre-registered experimental protocol

**Status: protocol executed once on mock components (see `results.md`, generated).** Numbers will be added to `experiments/results/` by the scripts in
`experiments/`, never by hand. This document is fixed *before* data collection so analysis choices cannot be tuned to
the outcome; any later change is recorded in the change log at the bottom.

## Research question
Can a cheap, rule-based query classifier that selects a retrieval plan and escalates only when retrieval confidence is
low match the answer-evidence quality of a strong fixed pipeline at lower latency and LLM cost?

## Systems compared (same corpus, embedder, reranker, LLM, `top_k`)
| ID | System |
|---|---|
| S1 | Dense only |
| S2 | Hybrid (dense + sparse, RRF) |
| S3 | Hybrid + rerank |
| S4 | Fixed "strong": hybrid + decompose + multi-query(3) + rerank |
| S5 | **Adaptive** (`AdaptivePolicy`, `max_retrieval_loops=2`) |
| S6 | Adaptive + knowledge-graph expansion (after Phase 5) |

## Hypotheses
* **H1 (quality):** S5 is non-inferior to S4 on nDCG@10 (margin Δ = 0.02 absolute).
* **H2 (cost):** S5 uses fewer LLM calls per query than S4 (median and mean).
* **H3 (latency):** S5 has lower mean and P95 retrieval latency than S4.
* **H4 (routing value):** On the *lookup* class, S5 matches or beats S2 in nDCG@10 while being cheaper than S3.
* **H5 (confidence signal):** Retrieval confidence is predictive of Recall@10 (AUROC > 0.65); if not, escalation is not
  justified and this is reported as a negative result.

## Data
`datasets/evaluation/`: ≥100 questions with manually verified gold evidence (document + page), stratified by class
(lookup, factoid, comparison, multi-hop, exploratory) and by temporal/version sensitivity. A held-out 30% split is used
for the only threshold/weight tuning allowed (`evidence_sufficiency_threshold`, confidence weights, RRF weights); the
final numbers are reported on the untouched 70%.

## Metrics
Retrieval: Precision@k, Recall@k, MRR, nDCG@k (k ∈ {5, 10}). Engineering: mean/P50/P95 latency, LLM calls, prompt and
completion tokens, estimated cost, failure rate. Classifier: per-class accuracy against hand labels, confusion matrix.

## Statistics
Paired per-query comparisons. 95% confidence intervals by paired bootstrap (10,000 resamples, fixed seed). Non-inferiority
tested on the CI lower bound of Δ nDCG@10. Wilcoxon signed-rank for per-class effects with Holm correction. Effect sizes are
always reported with their CIs, not only p-values.

## Controls and reproducibility
Fixed seeds; each run records git commit, dependency lock, embedding model id, reranker id, LLM id and temperature 0, and a
hash of the plan configuration. Latency is measured after one warm-up pass, median of 3 repeats. Same hardware for all systems.

## Threats to validity (must be discussed in the final report)
* **Mock components:** runs with the hashing embedder or `MockLLM` validate plumbing only; headline results require a real
  embedding model and real LLM, and both must be named in the report.
* The classifier and confidence weights are heuristic; their tuning split must stay separate from the reporting split.
* Corpus size/domain limits generalisation; small effects may not transfer to larger collections.
* LLM nondeterminism and provider updates can shift results between runs; record model ids and dates.
* Gold labels are author-written; report inter-annotator agreement if a second annotator is available.

## Change log
* (initial protocol)
* Execution deviations, recorded honestly:
  * **Data:** the benchmark is a *synthetic*, template-generated corpus (24 documents, 122 questions incl. 12 unanswerable) with gold derived by construction and checked by `corpus.verify`; it is **not** hand-annotated, so no inter-annotator agreement exists.
  * **Components:** run with the hashing embedder, lexical reranker and extractive `MockLLM` (no network). Headline claims therefore remain **unmade**; the protocol must be re-run with real models (`python -m app.evaluation` with real providers configured; the harness currently builds mock settings, pass `settings=` to `harness.run` for others).
  * **S6** is implemented as the adaptive ladder with graph expansion forced on at *every* level (the adaptive policy already enables it for multi-hop queries), so S5 vs S6 differs only on non-multi-hop classes.
  * **H5** label (Recall@10 = 1) was degenerate (every query succeeded) so AUROC is undefined; a gold-at-rank-1 AUROC is reported separately and labelled **post-hoc**.
  * **Tuning:** only the abstention gate (`abstain_confidence_threshold`, new) was tuned, on the dev split; the escalation threshold (0.35), confidence weights and RRF weights were **left at their defaults** and not tuned.
  * **Latency:** 3 repeats (median) after a warm-up pass, in-process ASGI transport on SQLite; absolute numbers are not comparable to a networked deployment.
