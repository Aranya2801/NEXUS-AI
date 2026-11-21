from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterIn(BaseModel):
    organization_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(max_length=256)
    full_name: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserCreateIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)
    role: Literal["admin", "editor", "viewer"] = "viewer"
    full_name: str = Field(default="", max_length=200)
    department: str | None = Field(default=None, max_length=100)


class UserOut(ORM):
    id: str
    org_id: str
    email: str
    full_name: str
    role: str
    department: str | None


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    is_restricted: bool = False


class CollectionOut(ORM):
    id: str
    name: str
    description: str
    is_restricted: bool


class AccessGrantIn(BaseModel):
    user_id: str


class JobOut(ORM):
    id: str
    document_id: str
    status: str
    stage: str
    attempts: int
    max_attempts: int
    error: str | None


class DocumentOut(ORM):
    id: str
    collection_id: str
    title: str
    filename: str
    doc_type: str
    department: str | None
    language: str | None
    status: str
    current_version: int
    created_at: datetime


class UploadOut(BaseModel):
    outcome: Literal["created", "new_version", "duplicate"]
    document: DocumentOut
    job: JobOut | None = None


class ChunkOut(ORM):
    id: str
    level: str
    kind: str = "text"
    ordinal: int
    text: str
    page_start: int | None
    page_end: int | None
    section: str | None
    char_start: int
    char_end: int
    token_count: int
    version: int
    is_current: bool
