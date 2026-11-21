"""Slack (Web API, bot token). One document per channel per UTC day. Incremental: `conversations.history?oldest=<cursor>` finds the
days that changed; those days are re-fetched in full so the document always holds the whole day."""
from __future__ import annotations

from datetime import datetime, timezone

from app.connectors.base import ConnectorAuthError, ConnectorError, RemoteItem, ScanResult
from app.connectors.http import SourceHttp

API = "https://slack.com/api"


def _day(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


class SlackConnector:
    kind, full_listing = "slack", False

    def __init__(self, http: SourceHttp, channels: list[str]) -> None:
        self.http, self.channels = http, channels
        self._users: dict[str, dict] | None = None

    async def _call(self, method: str, **params) -> dict:
        d = await self.http.json("GET", f"{API}/{method}", params=params)
        if not d.get("ok"):
            err = d.get("error", "unknown_error")
            if err in ("invalid_auth", "not_authed", "token_revoked", "account_inactive", "token_expired"):
                raise ConnectorAuthError(f"Slack rejected the token ({err})")
            raise ConnectorError(f"Slack API error: {err}")
        return d

    async def _load_users(self) -> dict[str, dict]:
        if self._users is None:
            self._users, cursor = {}, ""
            while True:
                d = await self._call("users.list", limit=200, **({"cursor": cursor} if cursor else {}))
                for m in d.get("members", []):
                    self._users[m["id"]] = {"name": m.get("real_name") or m.get("name") or m["id"], "email": (m.get("profile") or {}).get("email")}
                cursor = (d.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor:
                    break
        return self._users

    async def _history(self, channel: str, oldest: str | None = None, latest: str | None = None) -> list[dict]:
        out, cursor = [], ""
        while True:
            p: dict = {"channel": channel, "limit": 200}
            if oldest:
                p["oldest"] = oldest
            if latest:
                p["latest"] = latest
            if cursor:
                p["cursor"] = cursor
            d = await self._call("conversations.history", **p)
            out += [m for m in d.get("messages", []) if m.get("type") == "message" and not m.get("subtype") and m.get("text")]
            cursor = (d.get("response_metadata") or {}).get("next_cursor", "")
            if not d.get("has_more") or not cursor:
                return out

    async def _acl(self, channel: str) -> tuple[bool, list[str] | None, str]:
        info = (await self._call("conversations.info", channel=channel))["channel"]
        name = info.get("name") or channel
        if not (info.get("is_private") or info.get("is_im") or info.get("is_mpim")):
            return True, None, name                      # public channel: everyone in the workspace
        users, members, cursor = await self._load_users(), [], ""
        while True:
            d = await self._call("conversations.members", channel=channel, limit=200, **({"cursor": cursor} if cursor else {}))
            members += d.get("members", [])
            cursor = (d.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return False, sorted({users[u]["email"].lower() for u in members if u in users and users[u].get("email")}), name

    async def scan(self, cursor: str | None, limit: int) -> ScanResult:
        items, newest = [], cursor
        for ch in self.channels:
            msgs = await self._history(ch, oldest=cursor)
            if not msgs:
                continue
            public, readers, name = await self._acl(ch)
            for day in sorted({_day(m["ts"]) for m in msgs}):
                items.append(RemoteItem(f"{ch}:{day}", f"slack-{name}-{day}", ".txt", day, None, None, False, public, readers, {"channel": ch, "day": day}))
            newest = max([newest or "0"] + [m["ts"] for m in msgs], key=float)
        return ScanResult(items[:limit], newest, len(items) > limit)

    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]:
        day = datetime.strptime(item.hint["day"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start = day.timestamp()
        msgs = await self._history(item.hint["channel"], oldest=str(start), latest=str(start + 86400))
        users = await self._load_users()
        lines = [f"[{datetime.fromtimestamp(float(m['ts']), timezone.utc):%H:%M}] {users.get(m.get('user', ''), {}).get('name', m.get('user', 'unknown'))}: {m['text']}"
                 for m in sorted(msgs, key=lambda m: float(m["ts"]))]
        return "\n".join(lines).encode(), ".txt"

    async def close(self) -> None:
        await self.http.close()
