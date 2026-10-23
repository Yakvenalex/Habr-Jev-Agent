from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, text

from app.agent.tools.articles import clean_html
from app.api.auth import authorize
from app.db.models import Job, News, Publication, utcnow
from app.schemas.news import NewsDetail, NewsInput, NewsResult
from app.services.ingestion import Ingestion

router = APIRouter(prefix="/v1/news", dependencies=[Depends(authorize)])


@router.get("", summary="Последние 20 новостей для демонстрационного интерфейса")
async def recent_news(request: Request):
    async with request.app.state.db.sessions() as session:
        rows = await session.execute(
            select(News, Job.id)
            .outerjoin(Job, Job.news_id == News.id)
            .order_by(News.created_at.desc())
            .limit(20)
        )
        return {
            "items": [
                {
                    "id": news.id,
                    "job_id": job_id,
                    "title": news.title or "Без заголовка",
                    "category": news.category,
                    "status": news.status,
                    "reason": news.reason,
                    "created_at": news.created_at,
                }
                for news, job_id in rows
            ]
        }


@router.post("", response_model=NewsResult)
async def create_news(data: NewsInput, request: Request, response: Response):
    state = request.app.state
    result, status = await Ingestion(state.db, state.laya, state.settings).accept(data)
    response.status_code = status
    return result


@router.get("/{news_id}/article", summary="Сохранённая статья и результат публикации")
async def get_article(news_id: UUID, request: Request):
    async with request.app.state.db.sessions() as session:
        publication = await session.scalar(
            select(Publication).where(Publication.news_id == str(news_id))
        )
        if publication is None:
            raise HTTPException(404, "Article not saved yet")
        job = await session.get(Job, publication.job_id)
        return {
            "job_id": job.id,
            "can_publish": publication.status == "dry_run"
            and job.status not in {"queued", "processing"},
            "publish_pending": job.publish_requested and job.status in {"queued", "processing"},
            "title": publication.title,
            "summary": publication.summary,
            "content_html": clean_html(publication.content_html),
            "tags": publication.tags,
            "status": publication.status,
            "ghost_url": publication.ghost_url,
            "source_url": publication.source_url,
            "factuality_notes": publication.factuality_notes,
        }


@router.post(
    "/{news_id}/publish", status_code=202, summary="Проверить и опубликовать черновик через агента"
)
async def request_publication(news_id: UUID, request: Request, response: Response):
    async with request.app.state.db.sessions() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        publication = await session.scalar(
            select(Publication).where(Publication.news_id == str(news_id))
        )
        if publication is None:
            raise HTTPException(404, "Saved article not found")
        job = await session.get(Job, publication.job_id)
        if publication.status == "published":
            response.status_code = 200
            return {
                "id": str(news_id),
                "job_id": job.id,
                "status": "published",
                "ghost_url": publication.ghost_url,
            }
        if job.status in {"queued", "processing"}:
            if job.publish_requested:
                return {"id": str(news_id), "job_id": job.id, "status": job.status}
            raise HTTPException(409, "Wait until the current job finishes")
        if publication.status != "dry_run":
            raise HTTPException(409, "Only a validated dry-run draft can be submitted")
        # Reuse the one durable job per news item; keep its prior trace for the audit.
        job.publish_requested, job.status, job.attempts = True, "queued", 0
        job.current_step, job.error = 0, None
        job.lease_token, job.lease_until, job.finished_at = None, None, None
        job.agent_trace = list(job.agent_trace or []) + [
            {
                "event": "publish_requested",
                "requested_at": utcnow().isoformat(),
            }
        ]
        news = await session.get(News, str(news_id))
        news.status, news.reason = "queued", None
        await session.commit()
        return {"id": news.id, "job_id": job.id, "status": "queued"}


@router.get("/{news_id}", response_model=NewsDetail)
async def get_news(news_id: UUID, request: Request):
    async with request.app.state.db.sessions() as session:
        news = await session.get(News, str(news_id))
        if news is None:
            raise HTTPException(404, "News not found")
        return news
