from html import escape
from html.parser import HTMLParser

import nh3
from sqlalchemy import select, text

from app.agent.state import RejectArticle
from app.agent.tools.sources import require_lease
from app.db.models import News, Publication


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(html: str) -> str:
    parser = PlainText()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def clean_html(html: str) -> str:
    return nh3.clean(
        html,
        tags={
            "p",
            "br",
            "h2",
            "h3",
            "ul",
            "ol",
            "li",
            "strong",
            "em",
            "a",
            "blockquote",
            "pre",
            "code",
        },
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https"},
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


async def save_article(ctx, args):
    if ctx.state.publish_requested:
        raise RejectArticle("saved_draft_is_read_only")
    if args.needs_verification:
        raise RejectArticle("facts_require_verification")
    if not ctx.state.source_checked or not ctx.state.duplicates_checked:
        raise RejectArticle("source_and_duplicates_must_be_checked_first")
    html = clean_html(args.content_html)
    if len(plain_text(html)) < 100:
        raise RejectArticle("article_body_too_short_after_sanitization")
    if any(not tag.strip() or len(tag) > 80 for tag in args.tags):
        raise RejectArticle("invalid_tags")
    # Separate generated-content classification from the source category.
    category, _ = await ctx.laya.classify(args.title + "\n" + plain_text(html))
    if category.choice != "technology":
        raise RejectArticle("generated_article_not_technology")
    async with ctx.db.sessions() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        await require_lease(session, ctx.state)
        news = await session.get(News, ctx.state.news_id)
        if not news.source_url:
            raise RejectArticle("missing_identifiable_source_url")
        source = escape(news.source_url, quote=True)
        html += (
            f'<p>Источник: <a href="{source}" rel="noopener noreferrer">'
            f"{escape(news.source_name)}</a></p>"
        )
        publication = await session.scalar(
            select(Publication).where(Publication.news_id == news.id)
        )
        if publication and publication.status in {"published", "submitting"}:
            return {"publication_id": publication.id, "status": publication.status}
        values = dict(
            title=args.title,
            content_html=html,
            summary=args.summary,
            tags=args.tags,
            source_url=news.source_url,
            relevance_evidence=args.relevance_evidence,
            factuality_notes=args.factuality_notes,
            status="draft",
        )
        if publication:
            for key, value in values.items():
                setattr(publication, key, value)
        else:
            publication = Publication(
                news_id=news.id, job_id=ctx.state.job_id, slug="jev-" + news.id, **values
            )
            session.add(publication)
        await session.commit()
        return {"publication_id": publication.id, "status": "draft"}
