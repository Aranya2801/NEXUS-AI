"""Document submission (validation, dedupe, versioning) and job processing (parse → chunk → embed → swap)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.access import require_collection
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.config import Settings
from app.core.context import set_usage_context
from app.security import pii
from app.core.exceptions import IngestionError, ValidationFailed
from app.core.logging import get_logger
from app.database.base import new_id, utcnow
from app.database.models import Chunk, Document, DocumentVersion, IngestionJob
from app.document_processing.chunking import chunk_document
from app.document_processing.normalize import fingerprint, sha256_hex, sketch_similarity
from app.document_processing.parsers import parse_document
from app.embeddings.base import Embedder
from app.ingestion.validation import validate_upload
from app.multimodal.enrich import enrich_images
from app.services.storage import Storage

log = get_logger("nexus.ingestion")
NEAR_DUP_THRESHOLD = 0.9
EMBED_BATCH = 64


@dataclass(slots=True)
class SubmitResult:
    document: Document
    version: DocumentVersion | None
    job: IngestionJob | None
    outcome: str  # created | new_version | duplicate


class IngestionService:
    policies = None      # PolicyStore, set by main/worker (Phase 15)
    metrics = None

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], storage: Storage,
                 settings: Settings, embedder: Embedder, graph=None, vision=None) -> None:
        self.sf, self.storage, self.settings, self.embedder = session_factory, storage, settings, embedder
        self.vision = vision  # VisionModel | None
        self.graph = graph  # GraphIndexer | None

    # ------------------------------------------------------------------ submit
    async def submit(self, db: AsyncSession, p: Principal, collection_id: str, filename: str, data: bytes,
                     department: str | None = None) -> SubmitResult:
        col = await require_collection(db, p, collection_id, write=True)
        safe = validate_upload(filename, data, self.settings)
        digest = sha256_hex(data)
        key = safe.lower()

        # exact duplicate content anywhere in the collection (any filename)
        dup = (await db.execute(
            select(Document).join(DocumentVersion, DocumentVersion.document_id == Document.id).where(
                Document.org_id == p.org_id, Document.collection_id == col.id,
                DocumentVersion.content_hash == digest, DocumentVersion.is_current.is_(True))
        )).scalars().first()
        if dup:
            await audit(db, "document.duplicate", org_id=p.org_id, user_id=p.user_id, resource=dup.id)
            return SubmitResult(dup, None, None, "duplicate")

        doc = (await db.execute(select(Document).where(
            Document.org_id == p.org_id, Document.collection_id == col.id, Document.logical_key == key))).scalar_one_or_none()
        if doc is None:
            doc = Document(org_id=p.org_id, collection_id=col.id, logical_key=key, title=safe, filename=safe,
                           doc_type=safe.rsplit(".", 1)[-1].lower(), department=department or None,
                           status="pending", current_version=0, created_by=p.user_id)
            db.add(doc)
            await db.flush()
            version_no, outcome = 1, "created"
        else:
            latest = (await db.execute(select(DocumentVersion.version).where(
                DocumentVersion.document_id == doc.id).order_by(DocumentVersion.version.desc()))).scalars().first()
            version_no, outcome = (latest or 0) + 1, "new_version"
            if department:
                doc.department = department

        storage_key = f"{p.org_id}/{doc.id}/v{version_no}/{safe}"
        await self.storage.put(storage_key, data)
        # is_current stays False until the job succeeds, so a failed re-upload never hides the old version.
        ver = DocumentVersion(document_id=doc.id, org_id=p.org_id, version=version_no, content_hash=digest,
                              storage_key=storage_key, size_bytes=len(data), is_current=False, created_at=utcnow())
        db.add(ver)
        await db.flush()
        job = IngestionJob(org_id=p.org_id, document_id=doc.id, version_id=ver.id, status="queued",
                           max_attempts=self.settings.ingestion_max_attempts)
        db.add(job)
        if outcome == "created":
            doc.status = "processing"
        await db.flush()
        await audit(db, "document.upload", org_id=p.org_id, user_id=p.user_id, resource=doc.id,
                    version=version_no, size=len(data))
        return SubmitResult(doc, ver, job, outcome)

    async def delete_document(self, db: AsyncSession, org_id: str, doc: Document) -> None:
        """Removes a document with everything derived from it (versions, blobs, chunks, jobs, graph entries)."""
        versions = (await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc.id))).scalars().all()
        for v in versions:
            await self.storage.delete(v.storage_key)
        if self.graph is not None:
            await self.graph.clear_document(db, org_id, doc.id)
        for model in (Chunk, IngestionJob, DocumentVersion):   # explicit: SQLite does not enforce ON DELETE CASCADE
            await db.execute(delete(model).where(model.document_id == doc.id))
        await db.delete(doc)

    # ----------------------------------------------------------------- process
    async def process_job(self, job_id: str) -> str:
        """Runs one attempt. Returns the job's resulting status: succeeded | queued (retry) | dead."""
        async with self.sf() as db:
            # Atomic claim (compare-and-set on status) so several worker processes can share one queue table safely.
            claimed = await db.execute(update(IngestionJob).where(IngestionJob.id == job_id, IngestionJob.status == "queued")
                                       .values(status="running", stage="parsing", attempts=IngestionJob.attempts + 1))
            await db.commit()
            job = await db.get(IngestionJob, job_id, populate_existing=True)
            if job is None:
                return "dead"
            set_usage_context(job.org_id, None)    # graph-extraction LLM calls are metered to the document's tenant
            if claimed.rowcount == 0:      # finished, dead, or being processed by another worker: never run it twice
                return job.status
            try:
                await self._run(db, job)
                job.status, job.stage, job.error = "succeeded", "done", None
                await db.commit()
                return "succeeded"
            except Exception as exc:  # noqa: BLE001 - classified below
                await db.rollback()
                job = await db.get(IngestionJob, job_id)
                permanent = isinstance(exc, (ValidationFailed, IngestionError))
                job.error = f"{type(exc).__name__}: {exc}"[:2000]
                if permanent or job.attempts >= job.max_attempts:
                    job.status = "dead"
                    await db.execute(update(Document).where(Document.id == job.document_id, Document.current_version == 0)
                                     .values(status="failed"))
                    log.error("ingestion job dead", extra={"extra_fields": {"job": job_id, "error": job.error}})
                else:
                    job.status = "queued"
                    log.warning("ingestion job will retry", extra={"extra_fields": {"job": job_id, "attempt": job.attempts}})
                await db.commit()
                return job.status

    async def _apply_policy(self, doc: Document, cs) -> dict | None:
        """Org security policy on document content: optionally refuse documents containing secrets, redact PII before anything is embedded/stored."""
        if self.policies is None:
            return None
        pol = await self.policies.get(doc.org_id)
        if pol.pii_documents == "off" and not pol.reject_secrets_in_documents:
            return None
        m = self.metrics
        if pol.reject_secrets_in_documents:
            hit = {}
            for c in cs.children:
                for k, n in pii.counts(pii.detect(c.text, [k for k in pii.ALL_KINDS if k in pii.SECRET_KINDS])).items():
                    hit[k] = hit.get(k, 0) + n
            if hit:
                if m is not None:
                    m.inc("nexus_policy_blocks_total", "ingestion", "secrets")
                raise IngestionError(f"Rejected by security policy: document contains credentials/secrets ({', '.join(sorted(hit))}); remove them and re-upload")
        if pol.pii_documents == "off":
            return None
        total: dict[str, int] = {}
        for c in (*cs.parents, *cs.children):
            if pol.pii_documents == "redact":
                c.text, found = pii.redact(c.text, pol.pii_kinds)
            else:
                found = pii.counts(pii.detect(c.text, pol.pii_kinds))
            if c.level == "child":                      # count each occurrence once (parents repeat child text)
                for k, n in found.items():
                    total[k] = total.get(k, 0) + n
        if total and m is not None:
            for k, n in total.items():
                m.inc("nexus_pii_findings_total", "ingestion", k, "redacted" if pol.pii_documents == "redact" else "flagged", by=n)
        return {"mode": pol.pii_documents, "findings": total} if total else None

    async def _run(self, db: AsyncSession, job: IngestionJob) -> None:
        ver = await db.get(DocumentVersion, job.version_id)
        doc = await db.get(Document, job.document_id)
        data = await self.storage.get(ver.storage_key)
        s = self.settings
        parsed = await asyncio.to_thread(
            parse_document, doc.filename, data, ocr_enabled=s.ocr_enabled, multimodal=s.multimodal_enabled,
            max_images=s.max_images_per_document, max_pixels=s.max_image_pixels, min_pixels=s.figure_min_pixels,
            pdf_tables=s.extract_pdf_tables)
        figures: list[dict] = []
        if parsed.images:
            job.stage = "images"
            figures = await enrich_images(parsed, s, self.vision, self.storage, f"{doc.org_id}/{doc.id}/v{ver.version}/figures")
        job.stage = "chunking"
        cs = await asyncio.to_thread(
            chunk_document, parsed, target_tokens=s.chunk_target_tokens, overlap_tokens=s.chunk_overlap_tokens,
            parent_tokens=s.parent_chunk_tokens)
        pii_meta = await self._apply_policy(doc, cs)
        if not cs.children:
            raise IngestionError("Document contains no extractable text (scanned? enable OCR, or configure a vision model for images)")
        job.stage = "embedding"
        vectors: list[list[float]] = []
        texts = [c.text for c in cs.children]
        for i in range(0, len(texts), EMBED_BATCH):
            vectors.extend(await self.embedder.embed(texts[i : i + EMBED_BATCH]))

        job.stage = "indexing"
        # idempotent on retry: drop any partial chunks for this version
        await db.execute(delete(Chunk).where(Chunk.version_id == ver.id))
        common = dict(org_id=doc.org_id, collection_id=doc.collection_id, document_id=doc.id, version_id=ver.id,
                      version=ver.version, is_current=False, embed_model=self.embedder.name, department=doc.department, doc_type=doc.doc_type)
        parent_ids = []
        for pc in cs.parents:
            pid = new_id()
            parent_ids.append(pid)
            db.add(Chunk(id=pid, level="parent", kind=pc.kind, ordinal=pc.ordinal, text=pc.text, page_start=pc.page_start,
                         page_end=pc.page_end, section=pc.section, char_start=pc.char_start, char_end=pc.char_end,
                         token_count=pc.token_count, **common))
        child_rows: list[Chunk] = []
        for cc, vec in zip(cs.children, vectors):
            row = Chunk(id=new_id(), level="child", kind=cc.kind, ordinal=cc.ordinal, text=cc.text, page_start=cc.page_start, page_end=cc.page_end,
                        section=cc.section, char_start=cc.char_start, char_end=cc.char_end, token_count=cc.token_count,
                        parent_id=parent_ids[cc.parent_ordinal] if cc.parent_ordinal is not None else None,
                        embedding=vec, **common)
            child_rows.append(row)
            db.add(row)

        graph_meta: dict | None = None
        if self.graph is not None and self.settings.graph_enabled:
            job.stage = "graph"
            try:
                extracted = await self.graph.extract(child_rows)  # pure: no DB writes, so failure leaves the transaction intact
            except Exception as exc:  # noqa: BLE001 - graph is an enrichment; the document must still become searchable
                graph_meta = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
                log.warning("graph extraction failed", extra={"extra_fields": {"document": doc.id, "error": graph_meta["error"]}})
            else:
                await db.flush()  # mentions/relations reference chunk ids
                graph_meta = {"status": "ok", **await self.graph.persist(db, doc, child_rows, extracted)}

        sketch = fingerprint(cs.full_text)
        near = await self._find_near_duplicate(db, doc, sketch)
        # atomic swap: old version stops being searchable at the same instant the new one starts
        now = utcnow()
        await db.execute(update(Chunk).where(Chunk.document_id == doc.id, Chunk.version_id != ver.id).values(is_current=False))
        await db.execute(update(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.id != ver.id,
                                                       DocumentVersion.is_current.is_(True)).values(is_current=False, superseded_at=now))
        await db.execute(update(Chunk).where(Chunk.version_id == ver.id).values(is_current=True))
        ver.is_current = True
        doc.current_version, doc.status = ver.version, "ready"
        doc.title, doc.language = parsed.title[:500], parsed.language
        doc.meta = {**parsed.metadata, "sketch": sketch, "chunk_count": len(cs.children), **({"figures": figures} if figures else {}), **({"graph": graph_meta} if graph_meta else {}),
                    **({"near_duplicate_of": near} if near else {}), **({"pii": pii_meta} if pii_meta else {})}

    async def _find_near_duplicate(self, db: AsyncSession, doc: Document, sketch: list[int]) -> dict | None:
        rows = (await db.execute(select(Document.id, Document.title, Document.meta).where(
            Document.org_id == doc.org_id, Document.collection_id == doc.collection_id,
            Document.id != doc.id, Document.status == "ready").limit(500))).all()
        best = max(((sketch_similarity(sketch, (r.meta or {}).get("sketch", [])), r) for r in rows),
                   key=lambda t: t[0], default=(0.0, None))
        return {"document_id": best[1].id, "title": best[1].title, "similarity": round(best[0], 3)} if best[1] and best[0] >= NEAR_DUP_THRESHOLD else None

    async def recover_stuck(self, older_than: timedelta = timedelta(minutes=15)) -> list[str]:
        """Failure recovery: jobs left 'running' by a crashed worker go back to 'queued'."""
        async with self.sf() as db:
            ids = list((await db.execute(select(IngestionJob.id).where(
                IngestionJob.status == "running", IngestionJob.updated_at < utcnow() - older_than))).scalars())
            if ids:
                await db.execute(update(IngestionJob).where(IngestionJob.id.in_(ids)).values(status="queued"))
                await db.commit()
            return ids
