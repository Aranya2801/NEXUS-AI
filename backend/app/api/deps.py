from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.config import Settings
from app.core.context import set_usage_context
from app.core.exceptions import AuthenticationError, PermissionDenied
from app.core.security import decode_access_token
from app.database.models import User
from app.ingestion.runner import JobRunner
from app.ingestion.service import IngestionService

_bearer = HTTPBearer(auto_error=False)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_ingestion(request: Request) -> IngestionService:
    return request.app.state.ingestion


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> Principal:
    if creds is None:
        raise AuthenticationError("Missing bearer token")
    claims = decode_access_token(settings, creds.credentials)
    user = await db.get(User, claims["sub"])
    # role/active/org are re-read from the DB on every request so revocation is immediate
    if user is None or not user.is_active or user.org_id != claims["org"]:
        raise AuthenticationError("Invalid or expired token")
    set_usage_context(user.org_id, user.id)     # LLM calls made while serving this request are metered to this tenant
    return Principal(user.id, user.org_id, user.role, user.email)


def require_role(role: str):
    async def dep(p: Principal = Depends(get_principal)) -> Principal:
        if not p.at_least(role):
            raise PermissionDenied(f"{role} role required")
        return p

    return dep


async def budget_guard(request: Request, p: Principal = Depends(get_principal)) -> Principal:
    """Fail fast (HTTP 429 `budget_exceeded`) on LLM-spending endpoints once the org's daily budget is gone. Inside a run, call sites degrade
    gracefully instead of failing, so this check is what makes exhaustion visible to the caller."""
    from app.core.exceptions import BudgetExceeded

    cap = request.app.state.llm.cfg.budget.daily_usd_per_org
    if cap and await request.app.state.llmops["recorder"].spent_today(p.org_id) >= cap:
        raise BudgetExceeded("Daily LLM budget for this organisation is exhausted")
    return p
