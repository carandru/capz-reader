from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SetupBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=256)


class LoginBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class BookEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = None
    series: str | None = Field(default=None, max_length=300)
    volume: str | None = Field(default=None, max_length=80)
    cover_page: int | None = Field(default=None, ge=1)
    favorite: bool | None = None
    archived: bool | None = None
    read_state: str | None = None
    tags: list[str] | None = None


class BulkBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    action: str
    value: Any = None


class ProgressBody(BaseModel):
    page: int | None = Field(default=None, ge=1)
    seconds: float = Field(default=0, ge=0, le=3600)
    epub_location: str | None = Field(default=None, max_length=512)
    epub_progress: float | None = Field(default=None, ge=0, le=100)


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CategoryEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    visible: bool | None = None
    sort_order: int | None = None


class CategoryDelete(BaseModel):
    move_to: str | None = None


class ImportBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)
    category: str | None = None
