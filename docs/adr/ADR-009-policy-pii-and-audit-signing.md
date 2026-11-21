# ADR-009: Org policy, PII handling and audit signing

**Status:** accepted (Phase 15)

## Decisions
1. **Policy is data, per org, validated by a strict pydantic model** (unknown keys/values rejected) and read through a short TTL cache. Defaults are inert so upgrading changes nothing silently.
2. **Redact at the earliest point that matters.** For documents that is before embedding, so PII never reaches the vector index, graph, search results or any model. For model calls it is the
   router (single choke point, covers features added later). Placeholders are numbered per call (`[EMAIL_1]`) so repeated values stay coherent.
3. **Block means no side effects**: a refused query creates no run, conversation message or LLM call; only an audit row with kinds/counts.
4. **Never log or echo detected values** – findings are kinds, counts and offsets; error messages name kinds only.
5. **Checksums over breadth**: Luhn for cards, mod-97 for IBANs, SSN area rules, conservative phones. We prefer missing some PII over destroying ordinary numbers in research documents.
6. **Audit signing by per-row HMAC**, not a hash chain: a chain needs a serialised per-org tail, which breaks with several API replicas (false "tampering" alarms). Cost: deletions are not detected.
7. **The standalone worker uses the same stack builder** (`llmops/stack.py`) as the API, so metering, routing and policy cannot silently differ between ingestion modes.

## Consequences
Detection recall is partial; compliance claims are explicitly not made; policy coverage differs by endpoint (documented matrix).
