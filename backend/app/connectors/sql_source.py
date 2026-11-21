"""Read-only SQL source: each row of an admin-defined SELECT becomes a document. Defence in depth: a lexical guard (single SELECT/WITH
statement, no write/DDL keywords) AND a read-only session at the database level. Dialects are allow-listed (default: postgresql)."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.connectors.base import ConnectorError, RemoteItem, ScanResult
from app.core.config import Settings
from app.core.exceptions import ValidationFailed

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|copy|grant|revoke|truncate|vacuum|call|exec|execute|merge|replace|set|into)\b", re.I)


def check_select_only(query: str) -> str:
    q = query.strip().rstrip(";").strip()
    if not q or ";" in q or "--" in q or "/*" in q:
        raise ValidationFailed("Query must be a single statement without comments")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise ValidationFailed("Only SELECT queries are allowed")
    if _FORBIDDEN.search(q):
        raise ValidationFailed("Query contains a forbidden keyword (read-only connector)")
    return q


def check_dialect(url: str, s: Settings) -> None:
    try:
        u = make_url(url)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed("Invalid database URL") from exc
    if u.get_backend_name() not in s.sql_connector_allowed_dialects:
        raise ValidationFailed(f"Database dialect '{u.get_backend_name()}' is not allowed (allowed: {', '.join(s.sql_connector_allowed_dialects)})")


class SqlConnector:
    kind, full_listing = "sql", True

    def __init__(self, url: str, query: str, id_column: str, text_columns: list[str] | None, title_column: str | None,
                 updated_column: str | None, readers: list[str] | None, public: bool, settings: Settings) -> None:
        check_dialect(url, settings)
        self.query, self.id_col, self.text_cols, self.title_col, self.upd_col = check_select_only(query), id_column, text_columns, title_column, updated_column
        self.readers, self.public, self.s, self.url = readers, public, settings, make_url(url)

    def _connect_args(self) -> tuple:
        be = self.url.get_backend_name()
        if be == "sqlite":
            return (self.url.set(query={**self.url.query, "mode": "ro", "uri": "true"}) if str(self.url.database).startswith("file:")
                    else make_url(f"sqlite:///file:{self.url.database}?mode=ro&uri=true")), {}
        url = self.url.set(drivername="postgresql+psycopg") if self.url.drivername == "postgresql" else self.url   # psycopg 3 is the installed driver
        return url, {"options": "-c default_transaction_read_only=on -c statement_timeout=30000"}

    def _read(self, limit: int) -> list[dict]:
        url, args = self._connect_args()
        engine = create_engine(url, connect_args=args)
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(f"SELECT * FROM ({self.query}) AS nexus_q LIMIT :n"), {"n": limit + 1}).mappings().all()
            return [dict(r) for r in rows]
        finally:
            engine.dispose()

    async def scan(self, cursor: str | None, limit: int) -> ScanResult:
        try:
            rows = await asyncio.to_thread(self._read, limit)
        except Exception as exc:  # noqa: BLE001 - never echo driver messages (they can contain connection details)
            raise ConnectorError(f"Database query failed ({type(exc).__name__})") from exc
        truncated = len(rows) > limit
        items = []
        for r in rows[:limit]:
            if self.id_col not in r:
                raise ConnectorError(f"id_column '{self.id_col}' is not in the result set")
            ext_id = str(r[self.id_col])
            title = str(r.get(self.title_col) or ext_id) if self.title_col else ext_id
            cols = self.text_cols or [c for c in r if c != self.id_col]
            body = "\n".join(f"{c}: {r[c]}" for c in cols if c in r and r[c] is not None)
            items.append(RemoteItem(ext_id, title[:80], ".txt", str(r.get(self.upd_col)) if self.upd_col and r.get(self.upd_col) else None,
                                    None, None, False, self.public, self.readers, {"body": body}))
        return ScanResult(items, None, truncated)

    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]:
        return item.hint["body"].encode(), ".txt"

    async def close(self) -> None:
        return None
