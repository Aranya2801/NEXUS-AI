"""A/B testing of prompt versions. A run is assigned to one variant per running experiment by hashing (experiment id, run id): stable, no state, and
independent across runs. The assigned variants are stored on the run so outcomes can be joined to them later (and so a resumed run keeps its prompts)."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Experiment


@dataclass(frozen=True)
class Assignment:
    experiment_id: str
    name: str
    task: str
    variant: str
    version: str                      # "builtin" or a prompt version number as text


def pick_variant(exp_id: str, unit: str, variants: list[dict]) -> dict:
    total = sum(float(v["weight"]) for v in variants)
    u = int(hashlib.sha256(f"{exp_id}:{unit}".encode()).hexdigest()[:12], 16) / float(16**12)
    acc = 0.0
    for v in variants:
        acc += float(v["weight"]) / total
        if u < acc:
            return v
    return variants[-1]


class ExperimentService:
    def __init__(self, sf: async_sessionmaker[AsyncSession], ttl: float = 10.0) -> None:
        self.sf, self.ttl = sf, ttl
        self._c: dict[str, tuple[float, list[dict]]] = {}

    def invalidate(self, org_id: str | None = None) -> None:
        for k in [k for k in self._c if org_id is None or k == org_id]:
            self._c.pop(k, None)

    async def running(self, org_id: str, db: AsyncSession | None = None) -> list[dict]:
        """`db`: reuse the caller's open session (never open a second one inside someone else's transaction)."""
        hit = self._c.get(org_id)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        if db is None:
            async with self.sf() as s:
                rows = (await s.execute(select(Experiment).where(Experiment.org_id == org_id, Experiment.status == "running"))).scalars().all()
        else:
            rows = (await db.execute(select(Experiment).where(Experiment.org_id == org_id, Experiment.status == "running"))).scalars().all()
        data = [{"id": r.id, "name": r.name, "task": r.task, "variants": r.variants} for r in rows]
        self._c[org_id] = (time.monotonic(), data)
        return data

    async def assign(self, org_id: str, run_id: str, db: AsyncSession | None = None) -> list[Assignment]:
        out = []
        for e in await self.running(org_id, db):
            v = pick_variant(e["id"], run_id, e["variants"])
            out.append(Assignment(e["id"], e["name"], e["task"], v["name"], str(v["prompt_version"])))
        return out


def pins_of(variants: dict) -> dict[str, str]:
    """{task: version} from a run's stored `variants` (experiment id -> assignment dict)."""
    return {a["task"]: str(a["version"]) for a in (variants or {}).values()}
