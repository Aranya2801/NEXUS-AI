"""Google Drive (REST v3). Auth: an OAuth access token supplied by the admin (the OAuth consent flow itself is out of scope).
Incremental sync by `modifiedTime`; trashed files propagate as deletions; permissions map to readers."""
from __future__ import annotations

from app.connectors.base import RemoteItem, ScanResult
from app.connectors.http import SourceHttp

BASE = "https://www.googleapis.com/drive/v3"
EXPORTS = {  # Google-native types cannot be downloaded; they are exported to a text form
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}
READER_ROLES = {"reader", "commenter", "writer", "fileOrganizer", "organizer", "owner"}
FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,size,trashed,webViewLink,permissions(type,role,emailAddress))"


def split_name(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    return (stem, "." + ext.lower()) if dot and stem and len(ext) <= 5 else (name, "")


class GoogleDriveConnector:
    kind, full_listing = "google_drive", False

    def __init__(self, http: SourceHttp, folder_id: str | None = None, allowed: tuple[str, ...] = ()) -> None:
        self.http, self.folder_id, self.allowed = http, folder_id, allowed

    async def scan(self, cursor: str | None, limit: int) -> ScanResult:
        q = ["mimeType != 'application/vnd.google-apps.folder'"]
        if cursor:
            q.append(f"modifiedTime >= '{cursor}'")          # >= : same-instant edits are never missed; content hashing dedupes
        if self.folder_id:
            q.append(f"'{self.folder_id}' in parents")
        items, token, newest, truncated = [], None, cursor, False
        while True:
            params = {"q": " and ".join(q), "fields": FIELDS, "pageSize": 100, "orderBy": "modifiedTime", "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            d = await self.http.json("GET", f"{BASE}/files", params=params)
            for f in d.get("files", []):
                mime, (stem, ext) = f.get("mimeType", ""), split_name(f["name"])
                if mime in EXPORTS:
                    ext = EXPORTS[mime][1]
                    stem = f["name"]
                if ext not in self.allowed and not f.get("trashed"):
                    continue
                perms = f.get("permissions")
                public = bool(perms) and any(p.get("type") in ("anyone", "domain") for p in perms)
                readers = None if perms is None else sorted({p["emailAddress"].lower() for p in perms
                                                            if p.get("emailAddress") and p.get("role") in READER_ROLES})
                items.append(RemoteItem(f["id"], stem, ext, f.get("modifiedTime"), f.get("webViewLink"), int(f["size"]) if f.get("size") else None,
                                        bool(f.get("trashed")), public, readers, {"mime": mime}))
                newest = max(newest or "", f.get("modifiedTime") or "") or newest
                if len(items) >= limit:
                    return ScanResult(items, newest, True)
            token = d.get("nextPageToken")
            if not token:
                return ScanResult(items, newest, truncated)

    async def fetch(self, item: RemoteItem) -> tuple[bytes, str]:
        mime = item.hint.get("mime", "")
        if mime in EXPORTS:
            r = await self.http.request("GET", f"{BASE}/files/{item.external_id}/export", params={"mimeType": EXPORTS[mime][0]})
        else:
            r = await self.http.request("GET", f"{BASE}/files/{item.external_id}", params={"alt": "media", "supportsAllDrives": "true"})
        return r.content, item.ext

    async def close(self) -> None:
        await self.http.close()
