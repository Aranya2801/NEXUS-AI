from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal
from app.core.config import Settings
from app.core.exceptions import AuthenticationError, Conflict, ValidationFailed
from app.core.security import create_access_token, hash_password, verify_password
from app.database.base import new_id, utcnow
from app.database.models import AuditLog, Organization, User
from app.security.auditsig import sign_row

_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _check_password(settings: Settings, pw: str) -> None:
    if len(pw) < settings.min_password_length:
        raise ValidationFailed(f"Password must be at least {settings.min_password_length} characters")


async def audit(db: AsyncSession, action: str, *, org_id: str | None = None, user_id: str | None = None,
                resource: str | None = None, **detail) -> None:
    row = AuditLog(id=new_id(), org_id=org_id, user_id=user_id, action=action, resource=resource, detail=detail, created_at=utcnow())
    row.sig = sign_row(row)
    db.add(row)


async def register_organization(db: AsyncSession, settings: Settings, *, org_name: str, email: str,
                                password: str, full_name: str = "") -> User:
    """Creates a NEW organisation and its first admin. Joining an existing org is invite-only
    (an admin creates the user), so nobody can self-enrol into someone else's tenant."""
    _check_password(settings, password)
    email = email.lower()
    if (await db.execute(select(User.id).where(User.email == email))).first():
        raise Conflict("Email already registered")
    if (await db.execute(select(Organization.id).where(Organization.name == org_name))).first():
        raise Conflict("Organization name already taken")
    org = Organization(name=org_name)
    db.add(org)
    await db.flush()
    user = User(org_id=org.id, email=email, full_name=full_name, hashed_password=hash_password(password, settings.bcrypt_rounds), role="admin")
    db.add(user)
    await db.flush()
    await audit(db, "auth.register", org_id=org.id, user_id=user.id)
    return user


async def create_user(db: AsyncSession, settings: Settings, admin: Principal, *, email: str, password: str,
                      role: str, full_name: str = "", department: str | None = None) -> User:
    _check_password(settings, password)
    email = email.lower()
    if (await db.execute(select(User.id).where(User.email == email))).first():
        raise Conflict("Email already registered")
    user = User(org_id=admin.org_id, email=email, full_name=full_name, role=role, department=department,
                hashed_password=hash_password(password, settings.bcrypt_rounds))
    db.add(user)
    await db.flush()
    await audit(db, "user.create", org_id=admin.org_id, user_id=admin.user_id, resource=user.id, role=role)
    return user


async def authenticate(db: AsyncSession, settings: Settings, *, email: str, password: str) -> tuple[str, User]:
    user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
    ok = verify_password(password, user.hashed_password if user else _DUMMY_HASH)  # equalise timing
    if not user or not ok or not user.is_active:
        await audit(db, "auth.login_failed", org_id=user.org_id if user else None, user_id=user.id if user else None)
        await db.commit()  # persist the audit row even though we raise
        raise AuthenticationError("Incorrect email or password")
    await audit(db, "auth.login", org_id=user.org_id, user_id=user.id)
    return create_access_token(settings, user_id=user.id, org_id=user.org_id, role=user.role), user
