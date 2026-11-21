# NEXUS AI Architecture

```mermaid
flowchart LR
  UI[React + TS UI] --> GW[FastAPI gateway]
  GW --> AUTH[Auth / RBAC]
  GW --> ING[Ingestion API]
  GW --> CHAT[Chat / Research API]
  ING --> Q[(Redis queue)]
  Q --> W[Workers: parse, chunk, embed, graph]
  W --> PG[(PostgreSQL + pgvector)]
  W --> S3[(MinIO)]
  CHAT --> LG[LangGraph engine]
  LG --> RET[Hybrid retrieval]
  LG --> KG[Knowledge graph]
  RET --> PG
  KG --> PG
  LG --> CK[(Checkpoints)]
  GW --> OBS[OTel / LangSmith / metrics]
```

## Retrieval-time security
Every retrieval query is built from a `Principal` (user, org, role, accessible collections). The
filter `org_id = :org AND collection_id IN (:allowed) AND is_current` is part of the SQL/vector
scan, so unauthorised chunks are never candidates.

## Ingestion data flow
upload → validate (size, extension, magic bytes) → hash → dedupe/version → store → job queued →
parse → section/page detection → parent/child chunking → embeddings → graph extraction → ready.
