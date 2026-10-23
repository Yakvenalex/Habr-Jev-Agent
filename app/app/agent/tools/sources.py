from sqlalchemy import select

from app.agent.state import LeaseLost
from app.db.models import Job, News, Publication, utcnow


async def require_lease(session, state):
    job = await session.scalar(
        select(Job).where(
            Job.id == state.job_id,
            Job.status == "processing",
            Job.lease_token == state.lease_token,
            Job.lease_until > utcnow(),
        )
    )
    if job is None:
        raise LeaseLost("Worker no longer owns this job")
    return job


async def get_source_context(ctx, _args):
    async with ctx.db.sessions() as session:
        await require_lease(session, ctx.state)
        news = await session.get(News, ctx.state.news_id)
        ctx.state.source_checked = True
        result = {
            "id": news.id,
            "title": news.title,
            "content": news.content,
            "source": {
                "type": news.source_type,
                "name": news.source_name,
                "external_id": news.source_external_id,
                "url": news.source_url,
            },
            "category": news.category,
            "probabilities": news.category_probabilities,
            "published_at": news.published_at.isoformat() if news.published_at else None,
        }
        if ctx.state.publish_requested:
            article = await session.scalar(
                select(Publication).where(Publication.job_id == ctx.state.job_id)
            )
            result["saved_article"] = (
                {
                    "title": article.title,
                    "summary": article.summary,
                    "content_html": article.content_html,
                    "tags": article.tags,
                    "factuality_notes": article.factuality_notes,
                    "relevance_evidence": article.relevance_evidence,
                }
                if article
                else None
            )
        return result
