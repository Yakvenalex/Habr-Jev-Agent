import asyncio
import logging
import signal
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import and_, or_, select, text, update

from app.agent.harness import run_agent
from app.agent.registry import ToolContext
from app.agent.state import AgentState, LeaseLost, RejectArticle
from app.agent.tools.publishing import publish_article
from app.clients.ghost import GhostClient
from app.clients.laya import LayaClient
from app.clients.llm import LLMClient
from app.config import Settings
from app.db.models import Job, News, Publication, uid, utcnow
from app.db.session import Database
from app.schemas.agent import EmptyArgs
from app.services.ingestion import Ingestion

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, settings, laya, llm, ghost):
        self.db, self.settings = db, settings
        self.laya, self.llm, self.ghost = laya, llm, ghost

    async def claim(self):
        now = utcnow()
        available = or_(
            and_(Job.status == "queued", or_(Job.lease_until.is_(None), Job.lease_until <= now)),
            and_(Job.status == "processing", Job.lease_until <= now),
        )
        async with self.db.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            exhausted = (
                await session.scalars(
                    select(Job).where(available, Job.attempts >= self.settings.job_max_attempts)
                )
            ).all()
            for job in exhausted:
                publication = await session.scalar(
                    select(Publication).where(Publication.job_id == job.id)
                )
                confirmed = publication and publication.status == "published"
                job.status = "published" if confirmed else "failed"
                job.finished_at, job.lease_token, job.lease_until = now, None, None
                job.error = None if confirmed else "attempt_limit_exhausted"
                news = await session.get(News, job.news_id)
                news.status, news.reason = job.status, job.error
            job = await session.scalar(
                select(Job)
                .where(
                    available,
                    Job.attempts < self.settings.job_max_attempts,
                )
                .order_by(Job.created_at)
                .limit(1)
            )
            if job:
                job.status, job.lease_token = "processing", uid()
                job.attempts += 1
                job.lease_until = now + timedelta(seconds=self.settings.job_lease_seconds)
                job.started_at, job.finished_at, job.error = now, None, None
                job.current_step = 0
                news = await session.get(News, job.news_id)
                news.status, news.reason = "processing", None
            await session.commit()
            return job

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.job_lease_seconds / 3)
            async with self.db.sessions() as session:
                result = await session.execute(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        Job.status == "processing",
                        Job.lease_token == job.lease_token,
                        Job.lease_until > utcnow(),
                    )
                    .values(
                        lease_until=utcnow() + timedelta(seconds=self.settings.job_lease_seconds)
                    )
                )
                await session.commit()
                if not result.rowcount:
                    raise LeaseLost("Job lease expired")

    async def finish(self, job, status, reason=None):
        async with self.db.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            current = await session.get(Job, job.id)
            if (
                current.lease_token != job.lease_token
                or current.status != "processing"
                or current.lease_until <= utcnow()
            ):
                raise LeaseLost("Job lease expired before finish")
            # Remote publication is authoritative even if a later checkpoint failed.
            publication = await session.scalar(
                select(Publication).where(Publication.job_id == job.id)
            )
            if publication and publication.status == "published":
                status, reason = "published", None
            current.status, current.error = status, reason
            current.lease_token = None
            current.lease_until = (
                utcnow() + timedelta(seconds=min(60, 2**job.attempts))
                if status == "queued"
                else None
            )
            current.finished_at = None if status == "queued" else utcnow()
            news = await session.get(News, job.news_id)
            news.status, news.reason = status, reason
            await session.commit()

    async def process(self, job):
        state = AgentState(
            job_id=job.id,
            news_id=job.news_id,
            lease_token=job.lease_token,
            trace=list(job.agent_trace or []),
            publish_requested=job.publish_requested,
        )
        state.trace.append({"event": "attempt", "attempt": job.attempts})
        ctx = ToolContext(self.db, self.settings, state, self.laya, self.ghost)
        try:
            async with self.db.sessions() as session:
                news = await session.get(News, job.news_id)
                publication = await session.scalar(
                    select(Publication).where(Publication.job_id == job.id)
                )
            if publication and publication.status in {"published", "submitting"}:
                await publish_article(ctx, EmptyArgs())
            else:
                if news.category is None:
                    await Ingestion(self.db, self.laya, self.settings).classify(
                        news.id, lease_token=job.lease_token
                    )
                    async with self.db.sessions() as session:
                        news = await session.get(News, job.news_id)
                    if news.category is None:
                        raise RuntimeError("classification_unavailable")
                    if news.status == "skipped":
                        await self.finish(job, "skipped", "not_relevant")
                        return
                conclusion = await run_agent(ctx, self.llm)
                async with self.db.sessions() as session:
                    publication = await session.scalar(
                        select(Publication).where(Publication.job_id == job.id)
                    )
                accepted = {"published"} if job.publish_requested else {"published", "dry_run"}
                if not publication or publication.status not in accepted:
                    reason = (
                        conclusion.reason
                        if conclusion.outcome == "skipped"
                        else "agent_finished_without_publication"
                    )
                    await self.finish(job, "skipped", reason)
                    return
            # Re-read because publish_article updates a separate session.
            async with self.db.sessions() as session:
                publication = await session.scalar(
                    select(Publication).where(Publication.job_id == job.id)
                )
                status = "published" if publication.status == "published" else "skipped"
            await self.finish(
                job, status, None if status == "published" else "auto_publish_disabled"
            )
        except LeaseLost:
            raise
        except RejectArticle as error:
            await self.finish(job, "skipped", str(error))
        except Exception as error:
            status = "queued" if job.attempts < self.settings.job_max_attempts else "failed"
            # Exception messages may contain URLs, payloads or credentials.
            await self.finish(job, status, type(error).__name__)

    async def run_once(self) -> bool:
        job = await self.claim()
        if not job:
            return False
        processing = asyncio.create_task(self.process(job))
        heartbeat = asyncio.create_task(self.heartbeat(job))
        try:
            done, _ = await asyncio.wait(
                {processing, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        except LeaseLost:
            logger.warning("Worker lost job lease; processing stopped")
        finally:
            for task in (processing, heartbeat):
                task.cancel()
            for task in (processing, heartbeat):
                with suppress(asyncio.CancelledError, LeaseLost):
                    await task
        return True


async def main():
    settings = Settings()
    db = Database(settings.database_url)
    laya, llm, ghost = LayaClient(settings), LLMClient(settings), GhostClient(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await db.initialize()
    worker = Worker(db, settings, laya, llm, ghost)
    try:
        while not stop.is_set():
            try:
                worked = await worker.run_once()
            except Exception as error:
                logger.error("Worker iteration failed: %s", type(error).__name__)
                worked = False
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
    finally:
        await laya.close()
        await llm.close()
        await ghost.close()
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
