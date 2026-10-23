import hashlib
from datetime import UTC, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError

from app.agent.state import LeaseLost
from app.db.models import Job, News, Publication, uid, utcnow
from app.schemas.news import NewsInput, NewsResult, normalize_text
from app.services.routing import should_queue


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    p = urlsplit(value)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    )
    host = (p.hostname or "").lower()
    if p.port and not (
        p.scheme == "https" and p.port == 443 or p.scheme == "http" and p.port == 80
    ):
        host += f":{p.port}"
    if ":" in (p.hostname or ""):
        host = f"[{p.hostname}]" + (f":{p.port}" if p.port else "")
    return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/") or "/", urlencode(query), ""))


def duplicate_conditions(data: NewsInput, content_hash: str, url: str | None):
    conditions = [News.content_hash == content_hash]
    if url:
        conditions.append(News.normalized_source_url == url)
    if data.source.external_id:
        conditions.append(
            and_(
                News.source_type == data.source.type,
                News.source_name == data.source.name,
                News.source_external_id == data.source.external_id,
            )
        )
    return or_(*conditions)


class Ingestion:
    def __init__(self, db, laya, settings):
        self.db, self.laya, self.settings = db, laya, settings

    async def accept(self, data: NewsInput) -> tuple[NewsResult, int]:
        digest = hashlib.sha256(normalize_text(data.content).casefold().encode()).hexdigest()
        url = normalize_url(str(data.source.url)) if data.source.url else None
        condition = duplicate_conditions(data, digest, url)
        async with self.db.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            existing = await session.scalar(select(News).where(condition).limit(1))
            if existing:
                # Resubmitting the same text with its missing source repairs the
                # stopped job, rather than trapping the user in duplicate detection.
                if (
                    url
                    and existing.content_hash == digest
                    and not existing.source_url
                    and existing.status == "skipped"
                    and existing.reason == "missing_identifiable_source_url"
                ):
                    job = await session.scalar(select(Job).where(Job.news_id == existing.id))
                    publication = await session.scalar(
                        select(Publication.id).where(Publication.news_id == existing.id)
                    )
                    conflict = await session.scalar(
                        select(News.id).where(News.normalized_source_url == url)
                    )
                    if job and job.status == "skipped" and publication is None and conflict is None:
                        existing.source_url, existing.normalized_source_url = (
                            str(data.source.url),
                            url,
                        )
                        existing.status, existing.reason = "queued", None
                        job.status, job.attempts, job.current_step = "queued", 0, 0
                        job.error, job.lease_token, job.lease_until = None, None, None
                        job.started_at, job.finished_at, job.publish_requested = None, None, False
                        job.agent_trace = list(job.agent_trace or []) + [
                            {
                                "event": "processing_restarted",
                                "reason": "source_url_restored",
                                "at": utcnow().isoformat(),
                            }
                        ]
                        await session.commit()
                        return NewsResult(
                            id=existing.id,
                            status="queued",
                            category=existing.category,
                            job_id=job.id,
                            reason="source_url_restored",
                            agent_started=True,
                        ), 202
                return NewsResult(
                    id=existing.id, status="duplicate", reason="already_processed"
                ), 200
            news = News(
                id=uid(),
                source_type=data.source.type,
                source_name=data.source.name,
                source_external_id=data.source.external_id,
                source_url=str(data.source.url) if data.source.url else None,
                normalized_source_url=url,
                title=data.title,
                content=data.content,
                content_hash=digest,
                language=data.language.lower(),
                published_at=data.published_at.astimezone(UTC).replace(tzinfo=None)
                if data.published_at
                else None,
            )
            session.add(news)
            job = Job(
                news_id=news.id,
                status="processing",
                lease_token="ingestion",
                lease_until=utcnow()
                + timedelta(seconds=self.settings.external_timeout_seconds + 30),
            )
            # Flush parent first for the foreign key, then commit news and job atomically.
            try:
                await session.flush()
                session.add(job)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(select(News).where(condition).limit(1))
                if not existing:
                    raise
                return NewsResult(
                    id=existing.id, status="duplicate", reason="already_processed"
                ), 200
        return await self.classify(news.id, ingestion=True, lease_token=job.lease_token)

    async def classification_job(self, session, news_id, lease_token):
        await session.execute(text("BEGIN IMMEDIATE"))
        job = await session.scalar(select(Job).where(Job.news_id == news_id))
        if (
            job is None
            or job.lease_token != lease_token
            or job.status != "processing"
            or job.lease_until <= utcnow()
        ):
            raise LeaseLost("Classification lease expired")
        return job

    async def classify(
        self,
        news_id: str,
        ingestion: bool = False,
        lease_token: str | None = None,
    ) -> tuple[NewsResult, int]:
        async with self.db.sessions() as session:
            news = await session.get(News, news_id)
            text = "\n".join(filter(None, [news.title, news.content]))
        try:
            result, raw = await self.laya.classify(text)
        except Exception as error:
            async with self.db.sessions() as session:
                job = await self.classification_job(session, news_id, lease_token)
                news = await session.get(News, news_id)
                news.reason = "classification_unavailable"
                if isinstance(getattr(error, "raw", None), dict):
                    news.laya_response = error.raw
                if ingestion:
                    news.status = "queued"
                    job.status, job.lease_token, job.lease_until = "queued", None, None
                await session.commit()
            return NewsResult(
                id=news_id,
                status="queued",
                job_id=job.id,
                reason="classification_retry",
                agent_started=False,
            ), 202
        async with self.db.sessions() as session:
            job = await self.classification_job(session, news_id, lease_token)
            news = await session.get(News, news_id)
            news.category, news.category_probabilities = result.choice, result.probabilities
            news.laya_response, news.reason = raw, None
            if should_queue(result, self.settings):
                news.status = "queued"
                if ingestion:
                    job.status, job.lease_until, job.lease_token = "queued", None, None
                await session.commit()
                return NewsResult(
                    id=news_id,
                    status="queued",
                    category=result.choice,
                    job_id=job.id,
                    agent_started=True,
                ), 202
            news.status, news.reason = "skipped", "not_relevant"
            if ingestion:
                await session.delete(job)
            await session.commit()
            return NewsResult(
                id=news_id, status="skipped", category=result.choice, reason="not_relevant"
            ), 200
