# Evaluation

NEXUS ships an evaluation harness that measures retrieval quality, answer behaviour and cost from real runs of the real pipeline.
**Nothing in `experiments/results/` or `docs/research/results.md` is typed by hand.**

## Run it

```bash
cd backend
python -m app.evaluation --out ../experiments/results --repeats 3            # S1–S6 retrieval + end-to-end answers (test split)
python -m app.evaluation.calibrate ../experiments/results                     # tune the abstention gate on the dev split
python -m app.evaluation --out ../experiments/results --abstain-threshold calibrated
python ../experiments/render_report.py                                        # -> docs/research/results.md
```

The API serves the written files at `GET /evaluation/results` and `GET /evaluation/calibration`; the UI's *Evaluation* page renders them
(and shows an honest empty state with a 404 when no run exists).

## What is measured
* Retrieval: Precision@5/10, Recall@5/10, MRR, nDCG@5/10 at document level (chunks of one document collapse to its best rank).
* Engineering: mean/P50/P95 latency (median of repeats after warm-up), LLM calls, prompt tokens, failure rate.
* Answers (through the full 8-agent workflow): answer contains the gold facts, citation recall/precision, supported-claim rate,
  correct abstention on unanswerable questions, false abstention on answerable ones.
* Statistics: 95% paired bootstrap CIs (10,000 resamples, fixed seed), non-inferiority via CI lower bound, Wilcoxon signed-rank
  with Holm correction, AUROC. Implemented in `app/evaluation/metrics.py` and unit-tested against hand-computed values.

## The benchmark (`app/evaluation/corpus.py`)
Seeded and deterministic: 24 synthetic project documents and 122 questions — 22 each of lookup, factoid, comparison, multi-hop,
exploratory, plus 12 unanswerable. Facts are generated first and documents rendered from them, so gold is correct by construction;
`verify()` re-checks every gold label programmatically (and the tests prove it catches wrong gold). A stable hash assigns ~30% of
questions to **dev** (tuning allowed) and ~70% to **test** (reported).

**Limits you must keep in mind:** it is a template corpus, not enterprise text; it has no human annotation; it is small; and the
default run uses mock components, so it validates plumbing and *relative* strategy behaviour only.

## What the first (mock) run actually showed
Read the generated `docs/research/results.md` for numbers. Honest takeaways:
* Hybrid beats dense-only everywhere (S2 vs S1) and reranking helps further (S3).
* The adaptive policy (S5) was **non-inferior to the fixed strong pipeline** (S4) at roughly 40% of its LLM calls and about half its mean latency (H1–H3),
  and clearly better on multi-hop queries (graph expansion).
* **But the simple S3 (hybrid + rerank, zero LLM calls) had the highest overall nDCG@10 and the lowest cost.** Decomposition hurts
  comparison questions on this corpus (S4/S5 < S3). On this benchmark the adaptive policy's value is *relative to the expensive fixed
  pipeline*, not relative to a well-tuned simple one.
* The rule-based query classifier misses most exploratory phrasings (recall 0.36); it was deliberately not tuned on the benchmark.
* Retrieval confidence separated unanswerable from answerable questions well enough to calibrate an abstention gate (dev→test held), but the
  unanswerable set is small (n=9 in test) and the gate is specific to this backend — recalibrate per deployment.
* The extractive mock LLM cannot compose multi-hop answers (0.0 by construction). A real-LLM run is required for any claim about answer quality.

## Observability
`GET /admin/metrics` (admin only) exposes Prometheus-format request counters and latency histograms keyed by route *template*.
`NEXUS_OTEL_ENABLED=true` (+ `requirements-otel.txt`, `NEXUS_OTEL_ENDPOINT`) turns on OpenTelemetry tracing of every request;
`NEXUS_LANGSMITH_ENABLED=true` enables LangSmith tracing when `LANGSMITH_API_KEY` is set. Both degrade to a logged warning, never a crash,
and `GET /admin/observability` reports their state. Per-run agent traces (timings, plans, confidence) are always stored and visible in the
Run Inspector.
