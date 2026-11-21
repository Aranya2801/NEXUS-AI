# ADR-007: Autonomous agents — bounded autonomy and code execution

**Status:** accepted · **Phase:** 13

## Decision
- Autonomy is *bounded*: wave/step/wall-clock budgets are server settings a request can lower but never raise.
- Planner output is untrusted: validated against the live tool registry (and the caller's visible tools) before any step runs; invalid ⇒ rule planner.
- Tools execute with the **caller's** permissions (fresh session, owner re-validated each call); no tool can write to the knowledge base or the network.
- Answers are produced by the existing grounded pipeline; computed numbers are labelled, never cited.
- Code execution is a separate, default-off capability behind server flag + role + per-run opt-in, layered (AST, process, rlimits, timeout), audited by hash,
  and documented as **not** a hard boundary.

## Consequences
+ Autonomous runs are as evidence-bound as chat and fully inspectable. + A prompt-injected document cannot add tools or enable code.
− Rule planner is shallow; LLM planning quality is unmeasured. − Sandbox needs container isolation for hostile tenants. − Synchronous runs hold a request open.
