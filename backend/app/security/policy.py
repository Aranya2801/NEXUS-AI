"""Per-organisation security policy. Stored as JSON, validated on write, cached briefly. Defaults keep behaviour unchanged (everything off except
auditing of instruction-like user queries). Enforcement points: ingestion (documents), chat queries, outbound LLM prompts, delivered answers."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import NexusError
from app.database.base import utcnow
from app.database.models import OrgPolicy
from app.security.pii import ALL_KINDS, DEFAULT_KINDS


class PolicyViolation(NexusError):
    status_code, code = 400, "policy_violation"


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pii_kinds: list[Literal[*ALL_KINDS]] = Field(default_factory=lambda: list(DEFAULT_KINDS))
    pii_documents: Literal["off", "flag", "redact"] = "off"       # chunks are stored redacted -> never embedded, searched or shown
    reject_secrets_in_documents: bool = False                      # api keys / private keys / JWTs make the ingestion fail permanently
    pii_queries: Literal["off", "redact", "block"] = "off"
    pii_llm_prompts: Literal["off", "redact"] = "off"              # strip PII from text sent to the model provider
    pii_answers: Literal["off", "redact"] = "off"
    injection_queries: Literal["off", "flag", "block"] = "flag"


class PolicyStore:
    def __init__(self, sf: async_sessionmaker[AsyncSession], ttl: float = 10.0) -> None:
        self.sf, self.ttl = sf, ttl
        self._c: dict[str, tuple[float, Policy]] = {}

    async def get(self, org_id: str | None) -> Policy:
        if not org_id:
            return Policy()
        hit = self._c.get(org_id)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        async with self.sf() as db:
            row = await db.get(OrgPolicy, org_id)
        pol = Policy(**row.policy) if row else Policy()
        self._c[org_id] = (time.monotonic(), pol)
        return pol

    def invalidate(self, org_id: str | None = None) -> None:
        if org_id is None:
            self._c.clear()
        else:
            self._c.pop(org_id, None)

    async def put(self, db: AsyncSession, org_id: str, user_id: str, pol: Policy) -> None:
        row = await db.get(OrgPolicy, org_id)
        if row is None:
            db.add(OrgPolicy(org_id=org_id, policy=pol.model_dump(), updated_by=user_id, updated_at=utcnow()))
        else:
            row.policy, row.updated_by, row.updated_at = pol.model_dump(), user_id, utcnow()
        self._c.pop(org_id, None)
