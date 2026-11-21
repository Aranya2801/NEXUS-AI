"""Shared HTTP behaviour for source connectors: bearer auth, bounded retries honouring Retry-After, 401/403 -> auth error."""
from __future__ import annotations

import asyncio

import httpx

from app.connectors.base import ConnectorAuthError, ConnectorError


class SourceHttp:
    def __init__(self, client: httpx.AsyncClient | None, *, headers: dict[str, str] | None = None, max_retries: int = 3,
                 backoff: float = 0.5, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self._owns = client is None
        self.headers, self.max_retries, self.backoff, self.max_bytes = headers or {}, max_retries, backoff, max_bytes

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                r = await self.client.request(method, url, headers={**self.headers, **kw.pop("headers", {})}, **kw)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff * 2**attempt)
                    continue
                raise ConnectorError(f"Network error talking to source: {type(exc).__name__}") from exc
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                try:
                    wait = min(float(r.headers.get("Retry-After", "")), 30.0)
                except ValueError:
                    wait = self.backoff * 2**attempt
                await asyncio.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise ConnectorAuthError(f"Source rejected the credential ({r.status_code})")
            if r.status_code >= 400:
                raise ConnectorError(f"Source returned HTTP {r.status_code}")
            if len(r.content) > self.max_bytes:
                raise ConnectorError("Source object exceeds the size limit")
            return r
        raise ConnectorError("unreachable")

    async def json(self, method: str, url: str, **kw) -> dict:
        try:
            return self.request_json(await self.request(method, url, **kw))
        except ValueError as exc:
            raise ConnectorError("Source returned invalid JSON") from exc

    @staticmethod
    def request_json(r: httpx.Response) -> dict:
        d = r.json()
        if not isinstance(d, dict):
            raise ValueError("expected an object")
        return d

    async def close(self) -> None:
        if self._owns:
            await self.client.aclose()
