# AI security, governance and privacy (Phase 15)

Org admins manage a per-organisation **policy** (`GET/PUT /admin/security/policy`). Defaults leave behaviour unchanged, except that instruction-like chat queries are
*flagged* in the audit log. Every policy change is audited with a before/after diff.

| Setting | Values | Enforced at |
|---|---|---|
| `pii_kinds` | email, phone, ssn, credit_card, iban, ipv4, api_key, private_key, jwt (default: all except ipv4) | everywhere below |
| `pii_documents` | off / flag / redact | ingestion, **before** embedding, graph extraction and storage → redacted text is what is searched, shown and sent to models; counts kept in `document.meta.pii` |
| `reject_secrets_in_documents` | bool | ingestion: documents containing API keys / private keys / JWTs fail permanently (no retry; the value is not echoed) |
| `pii_queries` | off / redact / block | `POST /chat`: stored query and messages are redacted, or the request is refused (HTTP 400 `policy_violation`, no run created) |
| `injection_queries` | off / flag / block | `POST /chat`: instruction-like phrasing ("ignore previous instructions…") flagged or refused |
| `pii_llm_prompts` | off / redact | every LLM call (all features, incl. graph extraction and reports): the user part is redacted before it leaves for the provider |
| `pii_answers` | off / redact | delivered chat answers and citation quotes |

`POST /admin/security/scan` dry-runs the detectors on a sample and returns only kinds, counts and the *redacted* text.
`GET /admin/security/events` summarises policy events; `GET /admin/security/audit/verify` and `/audit/export` (CSV or JSONL, formula-injection safe) cover audit.

Retrieved-text prompt-injection defence from earlier phases (sentence filtering, data-block framing, claim verification) is unchanged.

## What this does NOT do
* **Detection is pattern-based.** No names, postal addresses, dates of birth, medical terms or non-US national IDs; phone detection is conservative (E.164 `+…`, parenthesised
  area codes, NANP 3-3-4). Expect misses and a few false positives. Not a compliance product (no GDPR/HIPAA/SOC 2 claim); put an NER/DLP service in front if you need more.
* **Coverage matrix.** Query policy and answer redaction apply to `/chat`; the autonomous agent (`/agentic`) and reports are covered by *prompt* redaction and document redaction only.
  Redaction applies to documents ingested *after* the policy is set – re-upload to re-process older ones. Char offsets of redacted chunks refer to the original text.
* **Redacted prompts can lower answer quality** (the model sees `[EMAIL_1]`), and verification quotes use the redacted text.
* **Policy changes take up to 10 s** to reach other API replicas (in-process cache).
* **Audit signing** is an HMAC per row keyed from `NEXUS_JWT_SECRET`: it detects *edited* rows. It cannot detect *deleted* rows and anyone with the secret can re-sign. Rows from before
  Phase 15 are reported as `unsigned`. For stronger guarantees ship exports to write-once storage. Rotating the JWT secret makes old rows verify as `tampered` – export first.
* The Python sandbox remains **not** a security boundary (see docs/agentic.md).
