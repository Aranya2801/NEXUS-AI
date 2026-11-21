from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ConnectorError(Exception):
    """Source-side failure (bad credentials, rate limiting exhausted, malformed response)."""


class ConnectorAuthError(ConnectorError):
    """401/403 from the source: the stored credential is invalid or expired (re-enter it; OAuth refresh is out of scope)."""


@dataclass(slots=True)
class RemoteItem:
    external_id: str
    name: str                      # display name WITHOUT extension
    ext: str                       # ".pdf", ".txt" ... must be in settings.allowed_extensions
    modified: str | None = None
    url: str | None = None
    size: int | None = None
    deleted: bool = False
    public: bool = False           # readable by everyone in the source tenant (anyone-link, org-wide link, public channel)
    readers: list[str] | None = None   # lowercase e-mails that can read it; None = the source did not tell us (unknown)
    hint: dict = field(default_factory=dict)   # connector-private data carried from scan() to fetch()


@dataclass(slots=True)
class ScanResult:
    items: list[RemoteItem]
    cursor: str | None             # new watermark to persist when the sync completes without failures
    truncated: bool = False        # True when the per-sync item cap stopped the scan early


class SourceConnector(Protocol):
    kind: str
    full_listing: bool             # True: scan() returns every item each time, so absent ones are deletions

    async def scan(self, cursor: str | None, limit: int) -> ScanResult: ...
    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]: ...   # (content, extension)
    async def close(self) -> None: ...
