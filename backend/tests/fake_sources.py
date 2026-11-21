"""In-memory fake Google Drive / Microsoft Graph / Slack / REST servers, served through httpx.MockTransport."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx


def js(data, status=200, headers=None):
    return httpx.Response(status, json=data, headers=headers)


class FakeWorld:
    def __init__(self) -> None:
        self.token = "good-token"
        self.calls: list[str] = []
        self.fail_next: dict[str, int] = {}      # path-substring -> number of 429s to emit first
        # Drive
        self.drive_files: list[dict] = []        # {id,name,mimeType,modifiedTime,trashed,content,perms}
        # Graph
        self.sp_items: dict[str, dict] = {}      # id -> {name,modified,content,perms,deleted}
        self.sp_phase = 0
        self.sp_pending_delta: list[str] = []
        self.teams_msgs: list[dict] = []
        self.teams_channel = {"membershipType": "standard"}
        self.teams_members = [{"email": "a@acme.io"}]
        # Slack
        self.slack_msgs: list[dict] = []         # {ts,user,text}
        self.slack_private = False
        self.slack_members = ["U1"]
        self.slack_users = {"U1": {"name": "Ana", "email": "a@acme.io"}, "U2": {"name": "Bo", "email": "b@acme.io"}}
        # REST
        self.rest_pages: dict[str, dict] = {}

    # ------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        u = urlparse(str(req.url))
        self.calls.append(f"{u.hostname}{u.path}")
        for frag, n in list(self.fail_next.items()):
            if frag in u.path and n > 0:
                self.fail_next[frag] = n - 1
                return httpx.Response(429, headers={"Retry-After": "0"})
        if u.hostname == "api.example.test":
            return self.rest(u, req)
        auth = req.headers.get("authorization", "")
        if auth != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "bad token"}) if u.hostname != "slack.com" else js({"ok": False, "error": "invalid_auth"})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.hostname == "www.googleapis.com":
            return self.drive(u, q)
        if u.hostname == "graph.microsoft.com":
            return self.graph(u, q)
        if u.hostname == "slack.com":
            return self.slack(u, q)
        return httpx.Response(404)

    # ---------------------------------------------------------------- Drive
    def drive(self, u, q):
        parts = u.path.split("/")
        if u.path.endswith("/files"):
            cond = q["q"]
            since = cond.split("modifiedTime >= '")[1].split("'")[0] if "modifiedTime >=" in cond else ""
            files = sorted([f for f in self.drive_files if f["modifiedTime"] >= since], key=lambda f: f["modifiedTime"])
            start = int(q.get("pageToken", 0))
            page = files[start:start + 2]                               # tiny pages -> exercises pagination
            body = {"files": [{k: f.get(k) for k in ("id", "name", "mimeType", "modifiedTime", "trashed")} | {"webViewLink": f"https://drive/{f['id']}",
                              **({"permissions": f["perms"]} if f.get("perms") is not None else {})} for f in page]}
            if start + 2 < len(files):
                body["nextPageToken"] = str(start + 2)
            return js(body)
        fid = parts[4]
        f = next((x for x in self.drive_files if x["id"] == fid), None)
        if f is None:
            return httpx.Response(404)
        return httpx.Response(200, content=f["content"].encode())

    # ---------------------------------------------------------------- Graph
    def graph(self, u, q):
        p = u.path
        if p.endswith("/root/delta"):
            if q.get("token") == "d1":                                   # second call: changes since the first delta link
                vals = []
                for i in self.sp_pending_delta:
                    it = self.sp_items[i]
                    vals.append({"id": i, "name": it["name"], "deleted": {}} if it.get("deleted") else
                                {"id": i, "name": it["name"], "file": {}, "lastModifiedDateTime": it["modified"], "webUrl": f"https://sp/{i}", "size": 10})
                return js({"value": vals, "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drv-1234/root/delta?token=d2"})
            if q.get("token") == "d2":
                return js({"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drv-1234/root/delta?token=d2"})
            ids = [i for i, it in self.sp_items.items() if not it.get("deleted")]
            first = [{"id": i, "name": self.sp_items[i]["name"], "file": {}, "lastModifiedDateTime": self.sp_items[i]["modified"],
                      "webUrl": f"https://sp/{i}", "size": 10} for i in ids[:1]] + [{"id": "fold", "name": "Folder", "folder": {}}]
            rest = [{"id": i, "name": self.sp_items[i]["name"], "file": {}, "lastModifiedDateTime": self.sp_items[i]["modified"],
                     "webUrl": f"https://sp/{i}", "size": 10} for i in ids[1:]]
            if q.get("page") == "2":
                return js({"value": rest, "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drv-1234/root/delta?token=d1"})
            return js({"value": first, "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/drv-1234/root/delta?page=2"})
        if p.endswith("/permissions"):
            iid = p.split("/")[-2]
            perms = self.sp_items.get(iid, {}).get("perms")
            if perms is None:
                return httpx.Response(403)
            return js({"value": perms})
        if p.endswith("/content"):
            iid = p.split("/")[-2]
            return httpx.Response(200, content=self.sp_items[iid]["content"].encode())
        if p.endswith("/members"):
            return js({"value": self.teams_members})
        if "/messages" in p:
            return js({"value": self.teams_msgs})
        if "/channels/" in p:
            return js(self.teams_channel)
        return httpx.Response(404)

    # ---------------------------------------------------------------- Slack
    def slack(self, u, q):
        m = u.path.rsplit("/", 1)[-1]
        if m == "conversations.history":
            oldest, latest = float(q.get("oldest", 0)), float(q.get("latest", 9e12))
            msgs = [{"type": "message", **x} for x in self.slack_msgs if oldest < float(x["ts"]) < latest]
            return js({"ok": True, "messages": sorted(msgs, key=lambda x: -float(x["ts"])), "has_more": False})
        if m == "conversations.info":
            return js({"ok": True, "channel": {"name": "general", "is_private": self.slack_private}})
        if m == "conversations.members":
            return js({"ok": True, "members": self.slack_members})
        if m == "users.list":
            return js({"ok": True, "members": [{"id": k, "name": v["name"], "profile": {"email": v["email"]}} for k, v in self.slack_users.items()]})
        return js({"ok": False, "error": "unknown_method"})

    # ---------------------------------------------------------------- REST
    def rest(self, u, req):
        key = u.path + ("?" + u.query if u.query else "")
        if req.headers.get("x-api-key") != "k-123":
            return httpx.Response(401)
        return js(self.rest_pages[key]) if key in self.rest_pages else httpx.Response(404)


def iso(y=2026, m=1, d=1, h=0, mi=0) -> str:
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(y, m, d, h=12, mi=0) -> str:
    return f"{datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp():.6f}"
