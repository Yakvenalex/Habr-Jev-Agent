from rapidfuzz import fuzz
from sqlalchemy import select

from app.db.models import News, Publication
from app.schemas.news import normalize_text


def similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0
    a, b = normalize_text(left).casefold(), normalize_text(right).casefold()
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))


async def matches(session, news, query="", published_only=False):
    statement = select(News, Publication).outerjoin(Publication, News.id == Publication.news_id)
    statement = statement.where(News.id != news.id)
    if published_only:
        # Uncertain submissions count as duplicates until reconciled.
        statement = statement.where(Publication.status.in_(["published", "submitting"]))
    results = []
    # Stream history: no unbounded ORM result materialization for a growing database.
    rows = await session.stream(statement)
    async for other, publication in rows:
        score = max(
            similarity(news.title, other.title),
            similarity(news.content, other.content),
            similarity(query, other.title),
        )
        if news.normalized_source_url and news.normalized_source_url == other.normalized_source_url:
            score = 100
        if score >= 50:
            results.append(
                {
                    "news_id": other.id,
                    "title": other.title,
                    "score": round(score, 2),
                    "status": publication.status if publication else other.status,
                    "url": publication.ghost_url if publication else None,
                }
            )
            results = sorted(results, key=lambda item: item["score"], reverse=True)[:10]
    return results


async def find_similar_news(ctx, args):
    from app.agent.tools.sources import require_lease

    async with ctx.db.sessions() as session:
        await require_lease(session, ctx.state)
        news = await session.get(News, ctx.state.news_id)
        results = await matches(session, news, args.query)
        ctx.state.duplicates_checked = True
        return {"matches": results, "threshold": ctx.settings.similar_news_threshold}
