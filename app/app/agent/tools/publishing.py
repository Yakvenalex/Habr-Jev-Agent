from datetime import timedelta

from sqlalchemy import func, select, text

from app.agent.state import PublicationUncertain, RejectArticle
from app.agent.tools.articles import clean_html, plain_text
from app.agent.tools.duplicates import matches
from app.agent.tools.sources import require_lease
from app.db.models import News, Publication, utcnow


async def store_result(ctx, post):
    if post.get("status") != "published" or not post.get("id") or not post.get("url"):
        raise PublicationUncertain("ghost_post_not_confirmed_published")
    async with ctx.db.sessions() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        await require_lease(session, ctx.state)
        publication = await session.scalar(
            select(Publication).where(Publication.job_id == ctx.state.job_id)
        )
        if post.get("slug") != publication.slug:
            raise PublicationUncertain("ghost_slug_mismatch")
        publication.ghost_post_id, publication.ghost_url = str(post["id"]), post["url"]
        publication.status, publication.published_at = "published", utcnow()
        await session.commit()
        return {"status": "published", "ghost_post_id": post["id"], "url": post["url"]}


async def publish_article(ctx, _args):
    async with ctx.db.sessions() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        await require_lease(session, ctx.state)
        publication = await session.scalar(
            select(Publication).where(Publication.job_id == ctx.state.job_id)
        )
        if not publication:
            raise RejectArticle("article_not_saved")
        if publication.status == "published":
            return {"status": "published", "url": publication.ghost_url}
        # A persisted submission is reconciled even after restart and irrespective of
        # the current AUTO_PUBLISH switch. This path never issues another POST.
        reconcile_only = publication.status == "submitting"
        if not reconcile_only:
            news = await session.get(News, ctx.state.news_id)
            if not news.category or news.status in {"failed", "skipped"} or news.reason:
                raise RejectArticle("source_not_eligible")
            if not ctx.state.source_checked or not ctx.state.duplicates_checked:
                raise RejectArticle("source_and_duplicates_must_be_checked_first")
            if not publication.source_url or not publication.title.strip():
                raise RejectArticle("source_or_title_missing")
            if len(plain_text(publication.content_html)) < 100:
                raise RejectArticle("empty_article")
            if clean_html(publication.content_html) != publication.content_html:
                raise RejectArticle("unsafe_html")
            similar = await matches(session, news, published_only=True)
            if any(item["score"] >= ctx.settings.similar_news_threshold for item in similar):
                raise RejectArticle("similar_publication_exists")
            if not ctx.settings.auto_publish:
                publication.status = "dry_run"
                await session.commit()
                return {"status": "dry_run", "reason": "auto_publish_disabled"}
            # Validate credentials before reserving a submission.
            ctx.ghost.headers()
            count = await session.scalar(
                select(func.count())
                .select_from(Publication)
                .where(Publication.submission_at >= utcnow() - timedelta(hours=1))
            )
            if count >= ctx.settings.max_posts_per_hour:
                raise RejectArticle("hourly_publication_limit")
            publication.status, publication.submission_at = "submitting", utcnow()
            await session.commit()

    # Reservation is durable before network I/O. Uncertain attempts are never reposted.
    post = await ctx.ghost.find(publication.slug)
    if post:
        return await store_result(ctx, post)
    if reconcile_only:
        raise PublicationUncertain("submission_unconfirmed_no_automatic_repost")
    try:
        post = await ctx.ghost.publish(publication)
    except Exception:
        # Includes transport failures and malformed successful responses. Do not
        # leak upstream response bodies or authorization data into job errors.
        try:
            post = await ctx.ghost.find(publication.slug)
        except Exception:
            raise PublicationUncertain("ghost_submission_unconfirmed") from None
        if not post:
            raise PublicationUncertain("ghost_submission_unconfirmed") from None
    return await store_result(ctx, post)
