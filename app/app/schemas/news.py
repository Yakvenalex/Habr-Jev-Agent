import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, field_validator


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["telegram", "rss", "website", "manual", "other"]
    name: str = Field(min_length=1, max_length=200)
    external_id: str | None = Field(None, min_length=1, max_length=256)
    url: HttpUrl | None = Field(None, max_length=2048)

    @field_validator("name", "external_id", mode="before")
    @classmethod
    def normalized(cls, value):
        return normalize_text(value) if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def no_credentials(cls, value):
        if value and (value.username or value.password):
            raise ValueError("Source URL must not include credentials")
        return value


class NewsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: Source
    title: str | None = Field(None, min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=20_000)
    language: str = Field("ru", min_length=2, max_length=16, pattern=r"^[a-zA-Z-]+$")
    published_at: AwareDatetime | None = None

    @field_validator("content", "title")
    @classmethod
    def not_blank(cls, value):
        if value is not None and not normalize_text(value):
            raise ValueError("Text must not be blank")
        return value


class NewsResult(BaseModel):
    id: str
    status: str
    category: str | None = None
    reason: str | None = None
    job_id: str | None = None
    agent_started: bool = False


class NewsDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str | None
    content: str
    source_type: str
    source_name: str
    source_external_id: str | None
    source_url: str | None
    language: str
    published_at: datetime | None
    category: str | None
    category_probabilities: dict | None
    status: str
    reason: str | None
    created_at: datetime
    updated_at: datetime
