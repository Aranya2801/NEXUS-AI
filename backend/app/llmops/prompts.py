"""Prompt versioning. Built-in prompts (app/llm/prompts.py) are the immutable `builtin` version and the rollback target. An org admin can
add numbered overrides per task and activate one; the router substitutes the active override at call time and records the version
and a hash with every call. Overrides must keep the `TASK: <name>` first line (it drives routing, metering and the offline mock)."""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import current_pins
from app.core.exceptions import NotFound, ValidationFailed
from app.database.base import utcnow
from app.database.models import PromptVersion
from app.llm import prompts as builtin_module

_TASK_LINE = re.compile(r"^TASK:\s*([a-z_]+)\s*$")
MAX_PROMPT_CHARS = 20_000


def builtin_prompts() -> dict[str, str]:
    out = {}
    for name in dir(builtin_module):
        v = getattr(builtin_module, name)
        if isinstance(v, str) and v.startswith("TASK:"):
            m = _TASK_LINE.match(v.split("\n", 1)[0])
            if m:
                out[m.group(1)] = v
    return out


def task_of(system: str) -> str:
    m = _TASK_LINE.match(system.split("\n", 1)[0].strip())
    return m.group(1) if m else "unknown"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class PromptStore:
    def __init__(self, sf: async_sessionmaker[AsyncSession], ttl: float = 15.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.sf, self.ttl, self.clock = sf, ttl, clock
        self._cache: dict[tuple[str, str], tuple[float, str, str | None]] = {}
        self.builtin = builtin_prompts()
        self._versions: dict[tuple[str, str, int], str | None] = {}

    async def _version_text(self, org_id: str, task: str, version: int) -> str | None:
        key = (org_id, task, version)
        if key not in self._versions:      # versions are immutable: cache without expiry
            async with self.sf() as db:
                self._versions[key] = (await db.execute(select(PromptVersion.text).where(PromptVersion.org_id == org_id, PromptVersion.task == task,
                                                                                      PromptVersion.version == version))).scalar_one_or_none()
        return self._versions[key]

    def invalidate(self, org_id: str | None = None) -> None:
        for k in [k for k in self._cache if org_id is None or k[0] == org_id]:
            self._cache.pop(k, None)

    async def resolve(self, org_id: str | None, task: str, system: str) -> tuple[str, str]:
        """Returns (system text to send, version label)."""
        if org_id is None or task not in self.builtin:
            return system, "builtin"
        pin = current_pins().get(task)            # an A/B experiment assigned this run a specific version
        if pin is not None:
            if pin == "builtin":
                return system, "builtin"
            text = await self._version_text(org_id, task, int(pin))
            if text:
                return text, str(pin)
        hit = self._cache.get((org_id, task))
        if hit is None or self.clock() - hit[0] > self.ttl:
            async with self.sf() as db:
                row = (await db.execute(select(PromptVersion).where(PromptVersion.org_id == org_id, PromptVersion.task == task,
                                                                     PromptVersion.is_active.is_(True)))).scalar_one_or_none()
            hit = (self.clock(), str(row.version) if row else "builtin", row.text if row else None)
            self._cache[(org_id, task)] = hit
        return (hit[2], hit[1]) if hit[2] else (system, "builtin")

    # ------------------------------------------------------------------ admin operations (caller commits)
    def validate(self, task: str, text: str) -> None:
        if task not in self.builtin:
            raise ValidationFailed(f"Unknown prompt task '{task}' (known: {', '.join(sorted(self.builtin))})")
        if len(text) > MAX_PROMPT_CHARS or not text.strip():
            raise ValidationFailed(f"Prompt must be 1-{MAX_PROMPT_CHARS} characters")
        if task_of(text) != task:
            raise ValidationFailed(f"Prompt must start with the line 'TASK: {task}'")

    async def create(self, db: AsyncSession, org_id: str, user_id: str, task: str, text: str, note: str, activate: bool) -> PromptVersion:
        self.validate(task, text)
        nxt = ((await db.execute(select(func.max(PromptVersion.version)).where(PromptVersion.org_id == org_id, PromptVersion.task == task))).scalar() or 0) + 1
        row = PromptVersion(org_id=org_id, task=task, version=nxt, text=text, note=note[:300], created_by=user_id, created_at=utcnow(), is_active=False)
        db.add(row)
        await db.flush()
        if activate:
            await self.activate(db, org_id, task, nxt)
        return row

    async def activate(self, db: AsyncSession, org_id: str, task: str, version: int | str) -> str:
        if task not in self.builtin:
            raise NotFound("Unknown prompt task")
        await db.execute(update(PromptVersion).where(PromptVersion.org_id == org_id, PromptVersion.task == task).values(is_active=False))
        label = "builtin"
        if version != "builtin":
            row = (await db.execute(select(PromptVersion).where(PromptVersion.org_id == org_id, PromptVersion.task == task,
                                                                 PromptVersion.version == int(version)))).scalar_one_or_none()
            if row is None:
                raise NotFound("Prompt version not found")
            row.is_active, label = True, str(row.version)
        self.invalidate(org_id)
        return label
