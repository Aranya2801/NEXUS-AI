"""Generic JSON REST source: each object in a list (found at `items_path`) becomes a document. Pagination follows a `next` URL but only
on the same host (a malicious response cannot redirect the crawler — or the auth header — elsewhere)."""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

from app.connectors.base import ConnectorError, RemoteItem, ScanResult
from app.connectors.http import SourceHttp
from app.connectors.security import assert_public_url
from app.core.config import Settings

MAX_PAGES = 50


def dig(obj, path: str | None):
    for part in (path or "").split("."):
        if part == "":
            continue
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


class RestConnector:
    kind, full_listing = "rest", True

    def __init__(self, http: SourceHttp, url: str, items_path: str | None, id_field: str, name_field: str | None, text_fields: list[str] | None,
                 updated_field: str | None, next_path: str | None, readers: list[str] | None, public: bool, settings: Settings,
                 allow_private: bool = False) -> None:
        self.http, self.url, self.items_path, self.id_f, self.name_f = http, url, items_path, id_field, name_field
        self.text_f, self.upd_f, self.next_path, self.readers, self.public = text_fields, updated_field, next_path, readers, public
        self.s, self.allow_private = settings, allow_private

    async def scan(self, cursor: str | None, limit: int) -> ScanResult:
        host, url, items = urlparse(self.url).netloc, self.url, []
        for _ in range(MAX_PAGES):
            assert_public_url(url, self.s, allow_private=self.allow_private)
            d = await self.http.json("GET", url)
            rows = dig(d, self.items_path) if self.items_path else d.get("items", d.get("data"))
            if not isinstance(rows, list):
                raise ConnectorError("items_path did not resolve to a list")
            for r in rows:
                if not isinstance(r, dict) or r.get(self.id_f) is None:
                    continue
                ext_id = str(r[self.id_f])
                cols = self.text_f or [k for k, v in r.items() if k != self.id_f and isinstance(v, (str, int, float))]
                body = "\n".join(f"{c}: {r[c]}" for c in cols if r.get(c) is not None)
                name = str(r.get(self.name_f) or ext_id) if self.name_f else ext_id
                items.append(RemoteItem(ext_id, name[:80], ".txt", str(r[self.upd_f]) if self.upd_f and r.get(self.upd_f) else None,
                                        None, None, False, self.public, self.readers, {"body": body}))
                if len(items) >= limit:
                    return ScanResult(items, None, True)
            nxt = dig(d, self.next_path) if self.next_path else None
            if not nxt:
                return ScanResult(items, None)
            url = urljoin(url, str(nxt))
            if urlparse(url).netloc != host:
                raise ConnectorError("Pagination link leaves the configured host; refusing to follow it")
        return ScanResult(items, None, True)

    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]:
        return item.hint["body"].encode(), ".txt"

    async def close(self) -> None:
        await self.http.close()
