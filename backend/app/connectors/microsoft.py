"""Microsoft Graph connectors: SharePoint/OneDrive document libraries (delta query) and Teams channel messages.
Auth: a Graph access token supplied by the admin. Delta/next links are only followed when they stay on graph.microsoft.com."""
from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.connectors.base import ConnectorError, RemoteItem, ScanResult
from app.connectors.google_drive import split_name
from app.connectors.http import SourceHttp

GRAPH = "https://graph.microsoft.com/v1.0"


def _graph_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != "graph.microsoft.com":
        raise ConnectorError("Refusing to follow a link that leaves graph.microsoft.com")
    return url


class SharePointConnector:
    kind, full_listing = "sharepoint", False

    def __init__(self, http: SourceHttp, drive_id: str, allowed: tuple[str, ...] = ()) -> None:
        self.http, self.drive_id, self.allowed = http, drive_id, allowed

    async def _readers(self, item_id: str) -> tuple[bool, list[str] | None]:
        try:
            d = await self.http.json("GET", f"{GRAPH}/drives/{self.drive_id}/items/{item_id}/permissions")
        except ConnectorError:
            return False, None                       # unknown -> strict mode will skip it (fail closed)
        public, emails = False, set()
        for p in d.get("value", []):
            if (p.get("link") or {}).get("scope") in ("anonymous", "organization"):
                public = True
            for ident in [p.get("grantedToV2")] + list(p.get("grantedToIdentitiesV2") or []):
                user = (ident or {}).get("user") or (ident or {}).get("siteUser") or {}
                if user.get("email"):
                    emails.add(user["email"].lower())
        return public, sorted(emails)

    async def scan(self, cursor: str | None, limit: int) -> ScanResult:
        url = _graph_url(cursor) if cursor else f"{GRAPH}/drives/{self.drive_id}/root/delta"
        items, delta = [], cursor
        while url:
            d = await self.http.json("GET", _graph_url(url))
            for v in d.get("value", []):
                if "folder" in v:
                    continue
                stem, ext = split_name(v.get("name", ""))
                if v.get("deleted") is not None:
                    items.append(RemoteItem(v["id"], stem or v["id"], ext or ".txt", deleted=True))
                    continue
                if ext not in self.allowed:
                    continue
                public, readers = await self._readers(v["id"])
                items.append(RemoteItem(v["id"], stem, ext, v.get("lastModifiedDateTime"), v.get("webUrl"), v.get("size"), False, public, readers))
                if len(items) >= limit:
                    return ScanResult(items, cursor, True)       # cursor NOT advanced: the remainder is picked up next run
            url = d.get("@odata.nextLink")
            delta = d.get("@odata.deltaLink", delta)
        return ScanResult(items, delta)

    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]:
        r = await self.http.request("GET", f"{GRAPH}/drives/{self.drive_id}/items/{item.external_id}/content", follow_redirects=True)
        return r.content, item.ext

    async def close(self) -> None:
        await self.http.close()


class TeamsConnector:
    """One document per channel per day (all messages of that day, oldest first)."""

    kind, full_listing = "teams", False

    def __init__(self, http: SourceHttp, team_id: str, channel_ids: list[str]) -> None:
        self.http, self.team_id, self.channel_ids = http, team_id, channel_ids

    async def _members(self, channel_id: str) -> tuple[bool, list[str] | None]:
        try:
            ch = await self.http.json("GET", f"{GRAPH}/teams/{self.team_id}/channels/{channel_id}")
            path = f"channels/{channel_id}/members" if ch.get("membershipType") in ("private", "shared") else "members"
            d = await self.http.json("GET", f"{GRAPH}/teams/{self.team_id}/{path}")
        except ConnectorError:
            return False, None
        return False, sorted({m["email"].lower() for m in d.get("value", []) if m.get("email")})

    async def _messages(self, channel_id: str, since: str | None) -> list[dict]:
        url, out = f"{GRAPH}/teams/{self.team_id}/channels/{channel_id}/messages?$top=50", []
        while url:
            d = await self.http.json("GET", _graph_url(url))
            out += [m for m in d.get("value", []) if not m.get("deletedDateTime") and m.get("messageType", "message") == "message"
                    and (not since or (m.get("lastModifiedDateTime") or m.get("createdDateTime") or "") >= since)]
            url = d.get("@odata.nextLink")
        return out

    async def scan(self, cursor: str | None, limit: int) -> ScanResult:
        items, newest = [], cursor
        for ch in self.channel_ids:
            changed_days: set[str] = set()
            for m in await self._messages(ch, cursor):
                ts = m.get("lastModifiedDateTime") or m.get("createdDateTime") or ""
                changed_days.add((m.get("createdDateTime") or ts)[:10])
                newest = max(newest or "", ts) or newest
            if changed_days:
                public, readers = await self._members(ch)
                for day in sorted(changed_days):
                    items.append(RemoteItem(f"{ch}:{day}", f"teams-{ch[:12]}-{day}", ".txt", day, None, None, False, public, readers, {"channel": ch, "day": day}))
        return ScanResult(items[:limit], newest, len(items) > limit)

    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]:
        msgs = [m for m in await self._messages(item.hint["channel"], None) if (m.get("createdDateTime") or "")[:10] == item.hint["day"]]
        lines = []
        for m in sorted(msgs, key=lambda m: m.get("createdDateTime", "")):
            who = ((m.get("from") or {}).get("user") or {}).get("displayName", "unknown")
            body = BeautifulSoup((m.get("body") or {}).get("content", ""), "lxml").get_text(" ", strip=True)
            if body:
                lines.append(f"[{(m.get('createdDateTime') or '')[11:16]}] {who}: {body}")
        return "\n".join(lines).encode(), ".txt"

    async def close(self) -> None:
        await self.http.close()
