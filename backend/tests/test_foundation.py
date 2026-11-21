import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.core.exceptions import AuthenticationError
from app.database.models import Chunk, Collection, Document, DocumentVersion, Organization, User
from app.database.base import utcnow


def test_password_roundtrip_and_long_password():
    long_pw = "x" * 200
    h = hash_password(long_pw)
    assert verify_password(long_pw, h)
    assert not verify_password(long_pw[:-1] + "y", h)  # beyond bcrypt's 72-byte limit still distinguished


def test_jwt_roundtrip_and_tamper(settings):
    tok = create_access_token(settings, user_id="u1", org_id="o1", role="admin")
    claims = decode_access_token(settings, tok)
    assert (claims["sub"], claims["org"], claims["role"]) == ("u1", "o1", "admin")
    with pytest.raises(AuthenticationError):
        decode_access_token(settings, tok[:-3] + "abc")


def test_prod_rejects_default_secret():
    with pytest.raises(ValueError):
        Settings(env="prod")


async def test_schema_and_traceable_chunk(db):
    org = Organization(name="Acme"); db.add(org); await db.flush()
    user = User(org_id=org.id, email="a@b.co", hashed_password="x"); col = Collection(org_id=org.id, name="kb")
    db.add_all([user, col]); await db.flush()
    doc = Document(org_id=org.id, collection_id=col.id, logical_key="f.txt", title="F", filename="f.txt", doc_type="txt")
    db.add(doc); await db.flush()
    ver = DocumentVersion(document_id=doc.id, org_id=org.id, version=1, content_hash="h", storage_key="k", created_at=utcnow())
    db.add(ver); await db.flush()
    db.add(Chunk(org_id=org.id, collection_id=col.id, document_id=doc.id, version_id=ver.id, version=1,
                 ordinal=0, text="hello", page_start=3, section="Intro", embedding=[0.1, 0.2]))
    await db.commit()
    c = (await db.execute(select(Chunk))).scalar_one()
    assert (c.page_start, c.section, c.embedding, c.is_current) == (3, "Intro", [0.1, 0.2], True)
