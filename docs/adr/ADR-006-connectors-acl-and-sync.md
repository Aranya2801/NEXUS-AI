# ADR-006: Connector permission model and sync semantics

**Status:** accepted · **Phase:** 12

## Context
Connectors pull content whose access rules live in another system, while NEXUS authorises per collection. A naive sync would silently widen
access (everyone with the collection can read a file only three people could read in Drive).

## Decision
1. Default **strict** ACL: ingest an item only when the set of NEXUS users with access to the target collection is a subset of the item's readers
   (or the item is public). Unknown ⇒ skip. Permission tightening ⇒ delete the synced copy.
2. Sources without per-item ACLs (SQL/REST) require the admin to *declare* readers; otherwise strict mode skips everything (explicit, visible in `/items`).
3. Sync is pull-based, single-flight per connector, with a watermark advanced only after a clean run, content-hash skipping and per-item transactions.
4. Content enters through `IngestionService.submit`, never a side path, so validation and versioning are identical to uploads.
5. Secrets are encrypted at rest; SQL is read-only at two layers; REST is SSRF-guarded and same-host.

## Consequences
+ No silent over-sharing; failures are visible per item. + One ingestion path to secure.
− Strict mode is unusable on a broad (unrestricted) collection by design; groups are not expanded; polling latency; metadata-only permission changes
need a resync; DNS-rebinding is not fully closed.
