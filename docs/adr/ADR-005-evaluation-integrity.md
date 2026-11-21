# ADR-005: Evaluation integrity rules

Status: accepted. Date: 2026-11-21.

1. **Results are produced only by code** (`app/evaluation`, `experiments/render_report.py`) and carry their environment; the generated report states loudly when
   mock components were used. No number in a README or doc is hand-typed.
2. **Synthetic gold is verified, not trusted:** facts are generated first, documents rendered from them, and `verify()` re-derives every gold label (tests prove it
   catches mislabelled gold). It is still not human annotation and is documented as such.
3. **Dev/test discipline:** thresholds may be tuned only on the dev split (30%); reported numbers come from the test split. Only one gate was tuned
   (`abstain_confidence_threshold`); other heuristics were deliberately left at their defaults and are listed as uncalibrated.
4. **Pre-registration with a change log:** deviations (degenerate H5 label, S6 definition, mock components) are recorded in the protocol, and post-hoc analyses are labelled.
5. **Negative and awkward results are reported**: the simple hybrid+rerank baseline (S3) beat both S4 and S5 overall on this benchmark; decomposition hurt comparison queries;
   the rule-based classifier misses most exploratory phrasings.
