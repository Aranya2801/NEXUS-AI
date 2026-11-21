from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db

router = APIRouter(tags=["system"])


@router.get("/health/live")
async def live():
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT 1"))
    return {"status": "ready", "workers": len(request.app.state.runner._tasks)}


@router.get("/health/version")
async def version():
    """What is running (for smoke tests after a deploy and for support). Unauthenticated; reveals only the SemVer string."""
    from app.version import __version__

    return {"version": __version__}
