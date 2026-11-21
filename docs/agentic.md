# Autonomous agentic systems (Phase 13)

`POST /agentic/runs {"goal": …}` runs a bounded plan → execute → observe → re-plan loop and returns the verified answer plus the full step trace
(also stored in `autonomous_runs`, Alembic 0004). `GET /agentic/tools` lists the tools **this caller** may use.

## Tools
| Tool | Role | Notes |
|---|---|---|
| `search_documents` | viewer | adaptive hybrid retrieval, caller's scope (ACL/versions) enforced in the query |
| `graph_lookup` | viewer | entities/relations from the knowledge graph |
| `read_document` | viewer | consecutive passages of a visible document; hidden = "document not found" |
| `calculator` | viewer | exact arithmetic via an AST evaluator (no names, bounded exponent/depth) |
| `python` | editor + opt-in | sandboxed pure-compute code; **off unless `NEXUS_AGENTIC_CODE_EXECUTION_ENABLED=true` and the run sets `allow_code`** |

Every call is schema-validated (unknown arguments rejected), role/flag-checked, time-boxed, output-capped, runs in its own DB session with the
owner re-validated, and is counted in Prometheus (`nexus_agentic_*`). Tool output is **data**: it reaches the planner fenced in `<data>` tags
and the answer only through sanitised passages.

## Planning
Each wave the planner returns steps (a small DAG; later steps may use `{"$ref": "s1.hits.0.document_id"}`). An LLM plan is accepted only if it validates
(known + visible tools, valid args, unique ids, known dependencies, step budget); otherwise a **deterministic rule planner** takes over (so behaviour
is defined offline): wave 0 = search (+graph for relationship wording, +calculator for explicit arithmetic); later waves widen thin evidence
(per-side searches for comparisons, keyword retry) or read the top document for summary goals. Independent steps run in parallel; failed steps are
recorded, dependants are skipped, the run continues.
Budgets: `agentic_max_waves` (3), `agentic_max_steps` (12), wall-clock `agentic_run_timeout_seconds` (60); `stopped_reason` says which one fired.

## Answer integrity
The final answer reuses the chat pipeline (sanitised passages → claims → claim verification → citation validation). Calculator/python results are
appended as a separate line "Computed by tools (not from documents)". They are never presented as citations. With no evidence the answer abstains.

## Code sandbox — what it is and is not
Layers: AST allow-list (no classes/async/generators, no dunder or `_private` attributes, no `eval/exec/open/getattr/type/…`, no `str.format`, imports
limited to `math statistics json re datetime decimal fractions itertools functools collections string random operator heapq bisect textwrap`);
separate interpreter (`-I -S`, empty environment, private temp cwd, own session) with RLIMIT CPU/AS/FSIZE/NOFILE/NPROC; restricted builtins +
allow-listed `__import__`; wall-clock kill of the process group; stdout cap. Tested: infinite loop, 10^10-element allocation, output flood, 28 escape
snippets, and the in-process layer called directly with code the validator would refuse. Audit logs record a hash of the code, never the code.

**It is not a security boundary against a determined attacker.** Python sandboxes built on the language itself have a long history of escapes, there
is no network namespace here, and RLIMIT_NPROC is per-user rather than per-process. For multi-tenant production run it inside a container/microVM
(no network, read-only root FS, seccomp, gVisor/Firecracker) — which is why it is disabled by default and requires three opt-ins (server flag, editor
role, per-run `allow_code`).

## Not verified
The LLM planning path was tested with a scripted planner and the offline mock, never a real model, so plan quality, prompt robustness and cost
with a real LLM are unknown. The rule planner's thresholds (0.35 confidence, ≥2 chunks) are uncalibrated heuristics. No autonomous-agent benchmark
exists in the evaluation harness. Runs are synchronous (no streaming/cancellation); there is no web-browsing or write-action tool by design.
