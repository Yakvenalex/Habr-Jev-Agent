from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    # SQLite stores naive timestamps; all stored values are UTC.
    return datetime.now(UTC).replace(tzinfo=None)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class News(Base):
    __tablename__ = "news"
    __table_args__ = (
        UniqueConstraint("source_type", "source_name", "source_external_id"),
        Index("ix_news_source", "source_type", "source_name"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_type: Mapped[str] = mapped_column(String(20))
    source_name: Mapped[str] = mapped_column(String(200))
    source_external_id: Mapped[str | None] = mapped_column(String(256), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    normalized_source_url: Mapped[str | None] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    language: Mapped[str] = mapped_column(String(16))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    category: Mapped[str | None] = mapped_column(String(20))
    category_probabilities: Mapped[dict | None] = mapped_column(JSON)
    laya_response: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), index=True, default="received")
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    news_id: Mapped[str] = mapped_column(ForeignKey("news.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(Text)
    agent_trace: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    news_id: Mapped[str] = mapped_column(ForeignKey("news.id"), unique=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    content_html: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source_url: Mapped[str] = mapped_column(Text)
    # These declarations are not a factual verification engine; see README.
    relevance_evidence: Mapped[str] = mapped_column(Text)
    factuality_notes: Mapped[str] = mapped_column(Text)
    ghost_post_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    ghost_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    submission_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
