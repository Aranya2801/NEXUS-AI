# Enterprise data connectors (Phase 12)

Connectors sync an external source into **one NEXUS collection**. Content flows through the normal ingestion path (validation, versioning,
dedupe, OCR, chunking, graph), so everything downstream (retrieval, citations, reports) works unchanged.

| Kind | Source | Incremental | Deletions | Permissions |
|---|---|---|---|---|
| `google_drive` | Drive API v3 | `modifiedTime >=` watermark | trashed files | file permissions (users/groups; `anyone`/`domain` = public) |
| `sharepoint` | Microsoft Graph drive **delta** | delta link | `deleted` facet | item permissions + sharing-link scope |
| `slack` | Web API (bot token) | `conversations.history?oldest` | n/a | public channel = public; private = member e-mails |
| `teams` | Graph channel messages | `lastModifiedDateTime` | n/a | team / private-channel members |
| `sql` | read-only SELECT (PostgreSQL by default) | full listing | rows no longer returned | declared in config (`readers`/`public`) |
| `rest` | JSON list endpoint | full listing (follows same-host `next`) | items no longer returned | declared in config |

Slack/Teams produce **one document per channel per day**; a changed day is re-fetched whole, so it becomes a new document version.

## Operating
`POST /connectors` (admin) → `POST /connectors/{id}/sync` (`?reset=true` forgets the watermark) → `GET /connectors/{id}/runs|items`.
Optional `interval_minutes` schedules syncs (scheduler loop in the API process). Runs are single-flight per connector; runs interrupted by a
crash are marked failed at startup. The watermark advances **only after a fully clean run** (item failures or a truncated scan keep it), and
unchanged content is skipped by SHA-256, so re-scans are cheap and safe.

## Permission model (read this)
NEXUS authorises at **collection** level; sources authorise per item. `acl_mode`:
- **`strict` (default)** — an item is ingested only if *every NEXUS user who can see the target collection* is allowed to read it at the source
  (or it is public there). Unknown permissions fail closed. If an item's source permissions later tighten, the synced copy is **deleted** on the next
  run. Practically: sync into a *restricted* collection and grant exactly the people who may read the source content.
- **`inherit`** — the collection's ACL applies to everything; use it only when the collection is already the intended audience.

Limits: group membership is not expanded (a Drive group address is compared as an e-mail), "organisation"/"domain" links are treated as public
within the tenant, and permission changes are noticed only when the item is re-scanned (modified) — a pure permission change on an unmodified
Drive file is not a modification. Run a full resync (`reset=true`) after tightening source permissions.

## Security decisions
- Credentials are **write-only**, Fernet-encrypted at rest (key from `NEXUS_CONNECTOR_SECRET_KEY`, else derived from the JWT secret — set a
  dedicated key in production), never returned or logged, rotatable with `PATCH {"secrets": …}`.
- **SSRF**: `rest` URLs must be http(s), carry no embedded credentials, and must not be (or resolve to) loopback/RFC1918/link-local/metadata
  addresses; pagination never leaves the configured host (so the auth header cannot be sent elsewhere). First-party connectors only talk to their
  fixed API hosts; Graph links are refused unless they stay on `graph.microsoft.com`. DNS rebinding between check and use is *not* prevented.
- **SQL**: single `SELECT`/`WITH` only (no comments, no write/DDL keywords) **and** a read-only database session (`default_transaction_read_only`,
  `statement_timeout`; SQLite opened `mode=ro`). Dialects are allow-listed — default PostgreSQL only (SQLite would expose server files). The
  keyword guard is conservative: `replace()`/`set` in a query are rejected. Driver error text is never echoed.
- Syncs run as the connector's creator (re-checked every run: deactivated or demoted owner ⇒ the run fails). Tenant isolation: other orgs get 404.
- Size cap per object (`NEXUS_CONNECTOR_MAX_ITEM_MB`) and per sync (`NEXUS_CONNECTOR_MAX_ITEMS_PER_SYNC`).

## What was and was not verified
Verified: every connector end-to-end through the API against **in-memory fake servers** (paging, delta links, 429 + Retry-After, 401 handling, deletion,
versioning, ACL gating), the SQL connector on SQLite **and a real PostgreSQL**, secrets-at-rest, SSRF guard, tenant isolation, migrations.
**Not verified: any real Google/Microsoft/Slack tenant.** Request shapes follow the public API documentation but real-world quirks (shared drives,
permission inheritance, Graph throttling, Slack scopes, large-file download redirects) are untested. OAuth consent/refresh is out of scope: supply
a token and rotate it before it expires (a 401/403 fails the run with an explicit message). No webhook/push — polling only.
