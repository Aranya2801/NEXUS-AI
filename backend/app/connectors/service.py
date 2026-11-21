"""Sync engine: scan -> ACL gate -> fetch -> ingest (through the normal upload path, so validation, versioning, dedupe, OCR and
graph all apply) -> deletion propagation. Runs with the permissions of the connector's creator, re-read on every run."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.principal import Principal
from app.auth.service import audit
from app.connectors import registry
from app.connectors.base import ConnectorAuthError, ConnectorError, RemoteItem
from app.connectors.security import decrypt_secrets
from app.core.config import Settings
from app.distributed.lease import MemoryLease, hold
from app.core.exceptions import NexusError
from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import Collection, CollectionAccess, Connector, ConnectorItem, ConnectorRun, Document, User
from app.ingestion.runner import JobRunner
from app.ingestion.service import IngestionService

log = get_logger("nexus.connectors")


def _filename(item: RemoteItem, ext: str) -> str:
    stem = re.sub(r"[^\w\- ]", "_", item.name).strip(" _")[:100] or "item"
    return f"{stem} [{hashlib.sha1(item.external_id.encode()).hexdigest()[:8]}]{ext}"     # stable + unique per external object


class ConnectorService:
    def __init__(self, sf: async_sessionmaker[AsyncSession], ingestion: IngestionService, runner: JobRunner, settings: Settings,
                 client_factory=None, lease=None) -> None:
        self.sf, self.ingestion, self.runner, self.s, self.client_factory = sf, ingestion, runner, settings, client_factory
        self.lease = lease or MemoryLease()      # cross-replica single-flight (Redis in multi-replica deployments)
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._sched: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        async with self.sf() as db:   # runs left "running" by a crashed process can never finish: fail them so a new sync may start
            for r in (await db.execute(select(ConnectorRun).where(ConnectorRun.status == "running"))).scalars():
                if await self.lease.is_held(f"connector-sync:{r.connector_id}"):
                    continue      # another replica is genuinely running it right now: leave it alone
                r.status, r.finished_at, r.error = "failed", utcnow(), "interrupted by restart"
            await db.commit()
        self._sched = asyncio.create_task(self._scheduler())

    async def stop(self) -> None:
        self._stop.set()
        if self._sched:
            self._sched.cancel()
            await asyncio.gather(self._sched, return_exceptions=True)
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def drain(self, timeout: float = 60.0) -> None:
        if self._tasks:
            await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout)

    def trigger(self, connector_id: str) -> None:
        t = asyncio.create_task(self.sync(connector_id))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _scheduler(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.s.connector_scheduler_seconds)
            try:
                async with self.sf() as db:
                    now = utcnow()
                    due = [c.id for c in (await db.execute(select(Connector).where(Connector.enabled.is_(True), Connector.interval_minutes.is_not(None)))).scalars()
                           if c.last_sync_at is None or now - c.last_sync_at >= timedelta(minutes=c.interval_minutes)]
                for cid in due:
                    if not self._locks.get(cid, asyncio.Lock()).locked():
                        self.trigger(cid)
            except Exception:  # noqa: BLE001
                log.exception("connector scheduler tick failed")

    # ------------------------------------------------------------ ACL gate
    async def _collection_readers(self, db: AsyncSession, col: Collection) -> set[str]:
        """E-mails of every active user who can see this collection inside NEXUS."""
        q = select(User.email).where(User.org_id == col.org_id, User.is_active.is_(True))
        if col.is_restricted:
            granted = select(CollectionAccess.user_id).where(CollectionAccess.collection_id == col.id)
            q = q.where((User.role == "admin") | User.id.in_(granted))
        return {e.lower() for e in (await db.execute(q)).scalars()}

    @staticmethod
    def _acl_ok(item: RemoteItem, readers: set[str]) -> tuple[bool, str]:
        if item.public:
            return True, "public at source"
        if item.readers is None:
            return False, "source did not report permissions (unknown ACL)"
        extra = readers - set(item.readers)
        return (not extra, "ok" if not extra else f"{len(extra)} NEXUS user(s) with collection access cannot read it at the source")

    # ------------------------------------------------------------ sync
    async def sync(self, connector_id: str) -> str | None:
        lock = self._locks.setdefault(connector_id, asyncio.Lock())
        if lock.locked():
            return None
        async with lock, hold(self.lease, f"connector-sync:{connector_id}", self.s.lease_ttl_seconds) as lease:
            if not lease.acquired:
                return None           # another replica is syncing this connector
            async with self.sf() as db:
                c = await db.get(Connector, connector_id)
                if c is None:
                    return None
                run = ConnectorRun(org_id=c.org_id, connector_id=c.id, started_at=utcnow(), stats={})
                db.add(run)
                await db.commit()
                run_id = run.id
            stats = {"scanned": 0, "ingested": 0, "unchanged": 0, "deleted": 0, "skipped_acl": 0, "failed": 0}
            status, error, conn = "succeeded", None, None
            try:
                conn, new_cursor = await self._run(connector_id, stats)
                if stats["failed"]:
                    status = "partial"
            except (ConnectorError, NexusError) as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                new_cursor = None
            except Exception as exc:  # noqa: BLE001
                log.exception("connector sync crashed")
                status, error = "failed", f"{type(exc).__name__}"[:500]
                new_cursor = None
            async with self.sf() as db:
                run = await db.get(ConnectorRun, run_id)
                run.status, run.finished_at, run.stats, run.error = status, utcnow(), stats, error
                c = await db.get(Connector, connector_id)
                if c is not None:
                    c.last_sync_at = utcnow()
                    if status == "succeeded" and new_cursor is not None:
                        c.cursor = new_cursor          # advanced only after a fully clean run
                    await audit(db, "connector.sync", org_id=c.org_id, user_id=c.created_by, resource=c.id, status=status, **stats)
                await db.commit()
            return status

    async def _run(self, connector_id: str, stats: dict):
        async with self.sf() as db:
            c = await db.get(Connector, connector_id)
            user = await db.get(User, c.created_by)
            if user is None or not user.is_active or user.org_id != c.org_id or not (user.role in ("admin", "editor")):
                raise ConnectorError("The connector's owner is no longer an active editor/admin")
            p = Principal(user.id, user.org_id, user.role, user.email)
            col = await db.get(Collection, c.collection_id)
            readers = await self._collection_readers(db, col)
            conn = registry.build(c.kind, c.config, decrypt_secrets(self.s, c.secret_enc), self.s, self.client_factory)
            cursor, acl_mode, org_id = c.cursor, c.acl_mode, c.org_id
        try:
            scan = await conn.scan(cursor, self.s.connector_max_items_per_sync)
            stats["scanned"] = len(scan.items)
            seen: set[str] = set()
            for item in scan.items:
                seen.add(item.external_id)
                try:
                    await self._process(connector_id, org_id, p, conn, item, readers, acl_mode, stats)
                except ConnectorAuthError:
                    raise
                except (ConnectorError, NexusError) as exc:
                    stats["failed"] += 1
                    await self._mark(connector_id, org_id, item, "failed", str(exc)[:300])
            if conn.full_listing and not scan.truncated and not stats["failed"]:
                await self._delete_unseen(connector_id, org_id, p, seen, stats)
            return conn, (None if scan.truncated else scan.cursor)    # a truncated scan never advances the watermark
        finally:
            await conn.close()

    async def _mark(self, connector_id: str, org_id: str, item: RemoteItem, status: str, detail: str | None, **kw) -> ConnectorItem:
        async with self.sf() as db:
            row = (await db.execute(select(ConnectorItem).where(ConnectorItem.connector_id == connector_id,
                                                                 ConnectorItem.external_id == item.external_id))).scalar_one_or_none()
            if row is None:
                row = ConnectorItem(org_id=org_id, connector_id=connector_id, external_id=item.external_id)
                db.add(row)
            row.name, row.url, row.status, row.detail, row.synced_at = item.name, item.url, status, detail, utcnow()
            row.external_acl = item.readers if not item.public else ["*"]
            for k, v in kw.items():
                setattr(row, k, v)
            await db.commit()
            return row

    async def _drop_document(self, db: AsyncSession, org_id: str, document_id: str | None) -> bool:
        doc = await db.get(Document, document_id) if document_id else None
        if doc is None or doc.org_id != org_id:
            return False
        await self.ingestion.delete_document(db, org_id, doc)
        return True

    async def _process(self, connector_id, org_id, p: Principal, conn, item: RemoteItem, readers: set[str], acl_mode: str, stats: dict) -> None:
        async with self.sf() as db:
            row = (await db.execute(select(ConnectorItem).where(ConnectorItem.connector_id == connector_id,
                                                                 ConnectorItem.external_id == item.external_id))).scalar_one_or_none()
            prev_doc, prev_hash, prev_status = (row.document_id, row.content_hash, row.status) if row else (None, "", "")
        if item.deleted:
            async with self.sf() as db:
                if await self._drop_document(db, org_id, prev_doc):
                    stats["deleted"] += 1
                await db.commit()
            await self._mark(connector_id, org_id, item, "deleted", None, document_id=None, content_hash="")
            return
        if acl_mode == "strict":
            ok, why = self._acl_ok(item, readers)
            if not ok:
                async with self.sf() as db:   # permissions tightened (or collection widened): previously synced content must not stay visible
                    await self._drop_document(db, org_id, prev_doc)
                    await db.commit()
                stats["skipped_acl"] += 1
                await self._mark(connector_id, org_id, item, "skipped_acl", why, document_id=None, content_hash="")
                return
        data, ext = await conn.fetch(item)
        digest = hashlib.sha256(data).hexdigest()
        if prev_status == "synced" and digest == prev_hash and prev_doc:
            stats["unchanged"] += 1
            return
        if not data.strip():
            await self._mark(connector_id, org_id, item, "failed", "empty content")
            stats["failed"] += 1
            return
        async with self.sf() as db:
            res = await self.ingestion.submit(db, p, (await db.get(Connector, connector_id)).collection_id, _filename(item, ext), data)
            await db.commit()
            doc_id, job = res.document.id, res.job
        if job:
            self.runner.enqueue(job.id)
        stats["ingested"] += 1
        await self._mark(connector_id, org_id, item, "synced", None, document_id=doc_id, content_hash=digest)

    async def _delete_unseen(self, connector_id, org_id, p, seen: set[str], stats: dict) -> None:
        async with self.sf() as db:
            rows = (await db.execute(select(ConnectorItem).where(ConnectorItem.connector_id == connector_id,
                                                                  ConnectorItem.status.in_(("synced", "skipped_acl", "failed"))))).scalars().all()
            for r in rows:
                if r.external_id not in seen:
                    if await self._drop_document(db, org_id, r.document_id):
                        stats["deleted"] += 1
                    r.status, r.document_id, r.content_hash = "deleted", None, ""
            await db.commit()
