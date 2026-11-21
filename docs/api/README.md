# API reference

> Generated from the app's OpenAPI schema by `infrastructure/scripts/export_openapi.py`; the full machine-readable schema is `openapi.json`. In dev/test mode interactive docs are served at `/docs`; they are disabled when `NEXUS_ENV=prod`.

Authentication: `Authorization: Bearer <JWT>` from `POST /auth/login`. Errors use one envelope: `{"error": {"code", "message", "details"}}` (FastAPI request-validation errors keep `detail`). Resources the caller may not see return **404**, never 403, so existence does not leak.

| Area | Method | Path | Summary |
|---|---|---|---|
| admin | GET | `/admin/audit` | Audit Log |
| admin | GET | `/admin/jobs` | Ingestion Jobs |
| admin | GET | `/admin/metrics` | Metrics |
| admin | GET | `/admin/observability` | Observability |
| admin | GET | `/admin/overview` | Overview |
| admin | GET | `/admin/users` | List Users |
| admin | PATCH | `/admin/users/{user_id}` | Update User |
| auth | POST | `/auth/login` | Login |
| auth | GET | `/auth/me` | Me |
| auth | POST | `/auth/register` | Register |
| auth | POST | `/auth/users` | Create User |
| chat | GET | `/agents/runs` | List Runs |
| chat | GET | `/agents/runs/{run_id}` | Inspect Run |
| chat | POST | `/agents/runs/{run_id}/resume` | Resume Run |
| chat | POST | `/chat` | Chat |
| chat | GET | `/conversations` | List Conversations |
| chat | DELETE | `/conversations/{conversation_id}` | Delete Conversation |
| chat | GET | `/conversations/{conversation_id}/messages` | Conversation Messages |
| evaluation | GET | `/evaluation/calibration` | Calibration |
| evaluation | GET | `/evaluation/results` | Results |
| feedback | POST | `/feedback` | Give Feedback |
| feedback | GET | `/feedback/{run_id}` | My Feedback |
| knowledge-base | GET | `/collections` | List Collections |
| knowledge-base | POST | `/collections` | Create Collection |
| knowledge-base | POST | `/collections/{collection_id}/access` | Grant Access |
| knowledge-base | POST | `/collections/{collection_id}/documents` | Upload Document |
| knowledge-base | GET | `/documents` | List Documents |
| knowledge-base | DELETE | `/documents/{document_id}` | Delete Document |
| knowledge-base | GET | `/documents/{document_id}` | Get Document |
| knowledge-base | GET | `/documents/{document_id}/chunks` | Document Chunks |
| knowledge-base | GET | `/documents/{document_id}/job` | Document Job |
| knowledge-graph | GET | `/graph/entities` | List Entities |
| knowledge-graph | GET | `/graph/entities/{entity_id}` | Entity Detail |
| knowledge-graph | POST | `/graph/query` | Graph Query |
| knowledge-graph | POST | `/graph/reindex/{document_id}` | Reindex |
| knowledge-graph | GET | `/graph/stats` | Graph Stats |
| memory | DELETE | `/memory` | Forget |
| memory | GET | `/memory` | List Memory |
| memory | GET | `/memory/documents/recent` | Recent Documents |
| memory | DELETE | `/memory/items/{item_id}` | Delete Item |
| memory | PUT | `/memory/preferences/{key}` | Set Preference |
| memory | GET | `/memory/research-contexts` | List Contexts |
| memory | POST | `/memory/research-contexts` | Create Context |
| memory | DELETE | `/memory/research-contexts/{context_id}` | Delete Context |
| memory | GET | `/memory/research-contexts/{context_id}` | Get Context |
| memory | POST | `/memory/research-contexts/{context_id}/findings` | Add Finding |
| memory | POST | `/memory/research-contexts/{context_id}/pin` | Pin Document |
| reports | GET | `/reports` | List Reports |
| reports | POST | `/reports` | Create Report |
| reports | DELETE | `/reports/{report_id}` | Delete Report |
| reports | GET | `/reports/{report_id}` | Get Report |
| reports | GET | `/reports/{report_id}/export` | Export Report |
| search | POST | `/search` | Search |
| search | POST | `/search/advanced` | Search Advanced |
| system | GET | `/health/live` | Live |
| system | GET | `/health/ready` | Ready |

56 operations.
