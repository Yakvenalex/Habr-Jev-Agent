from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from app.agent.tools.articles import clean_html
from app.api.auth import authorize
from app.db.models import Job, News, Publication
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
        return {
            "title": publication.title,
            "summary": publication.summary,
            "content_html": clean_html(publication.content_html),
            "tags": publication.tags,
            "status": publication.status,
            "ghost_url": publication.ghost_url,
            "source_url": publication.source_url,
            "factuality_notes": publication.factuality_notes,
        }


@router.get("/{news_id}", response_model=NewsDetail)
async def get_news(news_id: UUID, request: Request):
    async with request.app.state.db.sessions() as session:
        news = await session.get(News, str(news_id))
        if news is None:
            raise HTTPException(404, "News not found")
        return news
