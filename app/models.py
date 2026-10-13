"""Request and response schemas. Validation happens here, at the edge."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

Role = Literal["owner", "editor", "viewer"]
Visibility = Literal["team", "private"]


def _clean_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    cleaned = sorted({t.strip().lower() for t in tags if t.strip()})
    if len(cleaned) > 20:
        raise ValueError("At most 20 tags")
    if any(len(t) > 40 for t in cleaned):
        raise ValueError("Tags must be 40 characters or fewer")
    return cleaned


class UserCreate(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    name: str = Field(min_length=1, max_length=100)


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str
    created_at: datetime


class UserCreated(UserOut):
    api_key: str = Field(description="Shown once. Store it; only its hash is kept.")


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TeamOut(BaseModel):
    id: UUID
    name: str
    role: Role
    created_at: datetime


class MemberAdd(BaseModel):
    email: str
    role: Role = "editor"


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    name: str
    role: Role


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=100_000)
    tags: list[str] = Field(default_factory=list)
    visibility: Visibility = "team"

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v):
        return _clean_tags(v)


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=100_000)
    tags: list[str] | None = None
    visibility: Visibility | None = None

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, v):
        return _clean_tags(v)


class NoteOut(BaseModel):
    id: UUID
    team_id: UUID
    author_id: UUID
    title: str
    body: str
    tags: list[str]
    visibility: Visibility
    version: int
    created_at: datetime
    updated_at: datetime


class NotePage(BaseModel):
    items: list[NoteOut]
    next_cursor: str | None = Field(description="Pass as ?cursor= to get the next page; null at the end.")
