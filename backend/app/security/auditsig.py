"""Tamper evidence for audit rows: each row carries an HMAC-SHA256 over its canonical content, keyed from the server secret.
Detects EDITED rows (and rows inserted without the key). It does NOT detect deleted rows, and anyone holding the server secret can re-sign:
ship audit logs to write-once external storage (the export endpoint) if you need stronger guarantees."""
from __future__ import annotations

import hashlib
import hmac
import json

_key: bytes = b"nexus-dev-audit-key"


def set_key(secret: str) -> None:
    global _key
    _key = hmac.new(secret.encode(), b"nexus-audit-v1", hashlib.sha256).digest()


def canonical(row) -> bytes:
    created = row.created_at.isoformat() if row.created_at is not None else None
    return json.dumps([row.id, row.org_id, row.user_id, row.action, row.resource, row.detail, created], sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode()


def sign_row(row) -> str:
    return hmac.new(_key, canonical(row), hashlib.sha256).hexdigest()


def verify_row(row) -> str:
    """'ok' | 'unsigned' (pre-Phase-15 row) | 'tampered'"""
    if not row.sig:
        return "unsigned"
    return "ok" if hmac.compare_digest(row.sig, sign_row(row)) else "tampered"
