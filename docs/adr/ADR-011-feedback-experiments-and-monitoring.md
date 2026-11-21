# ADR-011: Feedback, experiments and quality monitoring

**Status:** accepted (Phase 17)

## Decisions
1. **Consent before admin access to content.** Feedback exposes a user's question/answer to admins only when the user opts in per rating; aggregates never need consent.
2. **Experiments are prompt-version A/Bs on the existing prompt store.** No second override mechanism: variants *are* Phase 14 prompt versions, pinned per run through the request context, stored on the run.
3. **Run-level randomisation by hash**: stateless, reproducible, independent across experiments; the price is that a conversation can mix variants and users contribute several runs (documented, not hidden).
4. **No auto-promotion.** The system reports evidence with confidence intervals, Holm-adjusted p-values and an `insufficient_data` state; concluding an experiment is an explicit, audited admin action.
5. **Monitoring uses what is already recorded** (no new instrumentation on the hot path), compares windows with conventional tests and requires both statistical and practical significance.
6. **Regression evaluation reuses the research harness's metric code** so numbers mean the same thing in both places, and stores runs so comparisons are reproducible.
7. **Never open a second DB session inside an open transaction** for assignment lookups (found when the in-memory SQLite test configuration lost uncommitted rows); the caller's session is passed in.

## Consequences
Evidence is only as good as the feedback volume; small orgs will mostly see `insufficient_data`. Alerts are proxies. No UI yet.
