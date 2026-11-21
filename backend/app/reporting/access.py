"""Access-time redaction. A report stores quotes and claims taken from documents; if the reader's access to a source has
since been revoked (collection restricted, document deleted), everything that cites it is hidden on read AND export."""
from __future__ import annotations

import copy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.access import accessible_collection_ids
from app.auth.principal import Principal
from app.database.models import Chunk, Document

HIDDEN_NOTE = "[content hidden: it cites a source you can no longer access]"


async def redact_for(db: AsyncSession, p: Principal, content: dict) -> dict:
    cites = content.get("citations", [])
    if not cites:
        return content
    allowed = await accessible_collection_ids(db, p)
    ids = [c["chunk_id"] for c in cites]
    visible = set((await db.execute(select(Chunk.id).where(Chunk.id.in_(ids), Chunk.org_id == p.org_id,
                                                           Chunk.collection_id.in_(allowed)))).scalars()) if allowed else set()
    hidden = {c["id"] for c in cites if c["chunk_id"] not in visible}
    if not hidden:
        return content
    out = copy.deepcopy(content)
    for c in out["citations"]:
        if c["id"] in hidden:
            c.update(hidden=True, title="[restricted source]", filename="", document_id="", chunk_id="", page_start=None, page_end=None,
                     section=None, quote="[source no longer accessible]", quotes=[])
    bad = lambda cites_: bool(set(cites_) & hidden)  # noqa: E731
    out["executive_summary"] = [({**i, "text": HIDDEN_NOTE, "citations": []} if bad(i["citations"]) else i) for i in out.get("executive_summary", [])]
    for sec in out["sections"]:
        blocks = []
        for b in sec["blocks"]:
            if b["type"] == "claim" and bad(b["citations"]):
                b = {"type": "note", "text": HIDDEN_NOTE}
            elif b["type"] == "list":
                b = {**b, "items": [({**i, "text": HIDDEN_NOTE, "citations": []} if bad(i["citations"]) else i) for i in b["items"]]}
            elif b["type"] == "table":
                b = {**b, "rows": [({"cells": ["[hidden]", HIDDEN_NOTE, "", ""], "citations": []} if bad(r["citations"]) else r) for r in b["rows"]]}
            blocks.append(b)
        sec["blocks"] = blocks
    doc_ids = [d["document_id"] for d in out.get("documents", [])]
    ok_docs = set((await db.execute(select(Document.id).where(Document.id.in_(doc_ids), Document.org_id == p.org_id,
                                                              Document.collection_id.in_(allowed)))).scalars()) if doc_ids and allowed else set()
    out["documents"] = [d for d in out["documents"] if d["document_id"] in ok_docs]
    out["redacted"] = True
    return out
