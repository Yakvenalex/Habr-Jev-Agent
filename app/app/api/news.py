from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.auth import authorize
from app.db.models import News
from app.schemas.news import NewsDetail, NewsInput, NewsResult
from app.services.ingestion import Ingestion

router = APIRouter(prefix="/v1/news", dependencies=[Depends(authorize)])


@router.post("", response_model=NewsResult)
async def create_news(data: NewsInput, request: Request, response: Response):
    state = request.app.state
    result, status = await Ingestion(state.db, state.laya, state.settings).accept(data)
    response.status_code = status
    return result


@router.get("/{news_id}", response_model=NewsDetail)
async def get_news(news_id: UUID, request: Request):
    async with request.app.state.db.sessions() as session:
        news = await session.get(News, str(news_id))
        if news is None:
            raise HTTPException(404, "News not found")
        return news
