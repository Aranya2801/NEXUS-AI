from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import schemas as s
from app.api.deps import get_db, get_settings_dep, require_role, get_principal
from app.auth import service
from app.auth.principal import Principal
from app.core.config import Settings
from app.database.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=s.UserOut, status_code=201)
async def register(body: s.RegisterIn, db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    return await service.register_organization(db, settings, org_name=body.organization_name, email=body.email,
                                               password=body.password, full_name=body.full_name)


@router.post("/login", response_model=s.TokenOut)
async def login(body: s.LoginIn, db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    token, _ = await service.authenticate(db, settings, email=body.email, password=body.password)
    return s.TokenOut(access_token=token, expires_in=settings.access_token_minutes * 60)


@router.get("/me", response_model=s.UserOut)
async def me(p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    return await db.get(User, p.user_id)


@router.post("/users", response_model=s.UserOut, status_code=201)
async def create_user(body: s.UserCreateIn, p: Principal = Depends(require_role("admin")),
                      db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    return await service.create_user(db, settings, p, email=body.email, password=body.password, role=body.role,
                                     full_name=body.full_name, department=body.department)
