import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from conftest import FakeLLM, article_args, successful_responses, tool_call
from sqlalchemy import select

from app.agent.registry import ToolContext
from app.agent.state import AgentState, LeaseLost
from app.agent.tools.articles import clean_html, save_article
from app.db.models import Job, News, Publication, utcnow
from app.db.session import Database
from app.schemas.agent import ArticleArgs
from app.worker import Worker


def worker(env):
    return Worker(env.db, env.settings, env.laya, env.llm, env.ghost)


@pytest.mark.parametrize("auto_publish", [False, True])
async def test_complete_agent_flow(env, queued, auto_publish):
    env.settings.auto_publish = auto_publish
    env.llm.responses = successful_responses()
    assert await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        publication = await session.scalar(select(Publication))
        assert job.status == ("published" if auto_publish else "skipped")
        assert publication.status == ("published" if auto_publish else "dry_run")
        assert "https://example.com/news/1" in publication.content_html
        assert job.current_step == 4
        assert len(job.agent_trace) >= 8
    assert env.ghost.post_calls == int(auto_publish)
    assert not await worker(env).run_once()


async def test_timeout_after_ghost_created_reconciles(env, queued):
    env.settings.auto_publish = True
    env.ghost.timeout_after_create = True
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).status == "published"
    assert env.ghost.post_calls == 1


async def test_uncertain_submission_never_reposts(env, queued):
    env.settings.auto_publish = True
    env.ghost.timeout_before_create = True
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.status == "queued"
        assert job.error == "PublicationUncertain"
        job.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    env.ghost.timeout_before_create = False
    await worker(env).run_once()
    assert env.ghost.post_calls == 1
    async with env.db.sessions() as session:
        assert (await session.scalar(select(Publication))).status == "submitting"


async def test_claim_is_exclusive(env, queued):
    jobs = await asyncio.gather(worker(env).claim(), worker(env).claim())
    assert sum(job is not None for job in jobs) == 1


async def test_restart_recovers_expired_job(env, queued):
    old = await worker(env).claim()
    async with env.db.sessions() as session:
        job = await session.get(Job, old.id)
        job.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    new_db = Database(env.settings.database_url)
    new_worker = Worker(new_db, env.settings, env.laya, env.llm, env.ghost)
    new = await new_worker.claim()
    assert new.id == old.id and new.attempts == 2 and new.lease_token != old.lease_token
    with pytest.raises(LeaseLost):
        await worker(env).finish(old, "published")
    await new_db.close()


async def test_exhausted_job_fails(env, queued):
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        job.status, job.attempts = "processing", env.settings.job_max_attempts
        job.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    assert await worker(env).claim() is None
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.status == "failed" and job.error == "attempt_limit_exhausted"


async def test_model_failure_retries_without_secret_in_errors(env, queued):
    env.llm.responses = [httpx.ReadTimeout("secret message and token")]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.status == "queued" and job.error == "ReadTimeout"
        assert "secret" not in json.dumps(job.agent_trace)


async def test_step_limit_is_terminal(env, queued):
    env.settings.agent_max_steps = 1
    env.llm.responses = [tool_call("get_source_context")]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.status == "skipped" and job.error == "agent_step_limit"


async def test_unknown_tool_and_invalid_args_are_not_executed(env, queued):
    env.llm.responses = [
        tool_call("shell", {"cmd": "rm -rf /"}),
        tool_call("publish_article", {"url": "https://evil.invalid"}),
        {
            "role": "assistant",
            "content": json.dumps({"outcome": "skipped", "reason": "Недостаточно информации"}),
        },
    ]
    await worker(env).run_once()
    assert env.ghost.post_calls == 0
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert [item["error"] for item in job.agent_trace if item.get("tool")] == [
            "unknown_tool",
            "invalid_tool_arguments",
        ]


async def test_publish_without_draft_is_rejected(env, queued):
    env.llm.responses = [tool_call("publish_article")]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).error == "article_not_saved"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"needs_verification": True}, "facts_require_verification"),
        (
            {"content_html": "<script>" + "a" * 120 + "</script>"},
            "article_body_too_short_after_sanitization",
        ),
    ],
)
async def test_article_fail_closed(env, queued, change, reason):
    args = article_args() | change
    env.llm.responses = successful_responses()[:2] + [tool_call("save_article", args)]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).error == reason
    assert env.ghost.post_calls == 0


async def test_missing_source_rejects(env, queued):
    async with env.db.sessions() as session:
        news = await session.get(News, queued.id)
        news.source_url = None
        await session.commit()
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).error == "missing_identifiable_source_url"


async def test_hourly_limit(env, queued):
    env.settings.auto_publish = True
    env.settings.max_posts_per_hour = 1
    job = await worker(env).claim()
    ctx = ToolContext(
        env.db,
        env.settings,
        AgentState(
            job.id, job.news_id, job.lease_token, source_checked=True, duplicates_checked=True
        ),
        env.laya,
        env.ghost,
    )
    await save_article(ctx, ArticleArgs.model_validate(article_args()))
    async with env.db.sessions() as session:
        pub = await session.scalar(select(Publication))
        pub.submission_at = utcnow()
        await session.commit()
    env.llm.responses = successful_responses()[:2] + [tool_call("publish_article")]
    await worker(env).process(job)
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).error == "hourly_publication_limit"


async def test_generated_nontech_article_rejected(env, queued):
    env.laya.category = "sports"
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).error == "generated_article_not_technology"


def test_html_sanitization():
    html = clean_html(
        '<script>alert(1)</script><p onclick="steal()">Text</p>'
        '<a href="javascript:steal()">x</a><img src=x onerror=steal()>'
    )
    assert "script" not in html and "onclick" not in html and "onerror" not in html
    assert "javascript:" not in html and "<img" not in html
    assert clean_html(html) == html


async def test_overall_timeout(env, queued):
    class SlowLLM(FakeLLM):
        async def complete(self, *_):
            await asyncio.sleep(10)

    env.settings.agent_timeout_seconds = 1
    env.llm = SlowLLM()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).error == "TimeoutError"


async def test_classification_recovery(env, payload):
    from app.schemas.news import NewsInput
    from app.services.ingestion import Ingestion

    env.laya.error = httpx.ReadTimeout("offline")
    result, _ = await Ingestion(env.db, env.laya, env.settings).accept(
        NewsInput.model_validate(payload)
    )
    env.laya.error = None
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        assert (await session.get(Job, result.job_id)).error == "auto_publish_disabled"


async def test_retry_can_complete_after_model_failure(env, queued):
    env.llm.responses = [httpx.ReadTimeout("offline")]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        job.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.attempts == 2 and job.error == "auto_publish_disabled"
        assert (await session.scalar(select(Publication))).status == "dry_run"


async def test_similar_published_news_cannot_be_republished(env, queued, payload):
    from app.schemas.news import NewsInput
    from app.services.ingestion import Ingestion

    env.settings.auto_publish = True
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    payload["source"]["external_id"] = "different-source-id"
    payload["source"]["url"] = "https://example.com/other-news"
    payload["content"] += " Новость перепечатана другим изданием."
    payload["title"] += " — обновление"
    result, code = await Ingestion(env.db, env.laya, env.settings).accept(
        NewsInput.model_validate(payload)
    )
    assert code == 202
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, result.job_id)
        assert job.status == "skipped" and job.error == "similar_publication_exists"
    assert env.ghost.post_calls == 1


async def test_stale_classification_cannot_mutate_news(env, queued):
    from app.services.ingestion import Ingestion

    job = await worker(env).claim()
    env.laya.category = "sports"
    with pytest.raises(LeaseLost):
        await Ingestion(env.db, env.laya, env.settings).classify(
            job.news_id, lease_token="stale-worker"
        )
    async with env.db.sessions() as session:
        assert (await session.get(News, job.news_id)).category == "technology"


async def test_protocol_correction_is_bounded_and_can_recover(env, queued):
    env.llm.responses = [{"role": "assistant", "content": "Сейчас подготовлю статью."}]
    env.llm.responses += successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.error == "auto_publish_disabled" and job.current_step == 5
        assert any(item.get("event") == "protocol_correction" for item in job.agent_trace)


async def test_crash_after_confirmed_publish_preserves_success(env, queued):
    env.settings.auto_publish = True
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        # Simulate a crash after publication persistence but before finishing the job.
        job.status, job.attempts = "processing", env.settings.job_max_attempts
        job.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    assert await worker(env).claim() is None
    async with env.db.sessions() as session:
        assert (await session.get(Job, queued.job_id)).status == "published"
    assert env.ghost.post_calls == 1


async def test_premature_completion_cannot_bypass_publish_checks(env, queued):
    env.llm.responses = successful_responses()[:3] + [
        {"role": "assistant", "content": '{"outcome":"completed","reason":"Draft saved"}'},
        tool_call("publish_article"),
    ]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.error == "auto_publish_disabled" and job.current_step == 5
        assert any(item.get("event") == "premature_completion" for item in job.agent_trace)


async def test_factuality_rejection_preserves_model_explanation(env, queued):
    notes = "Источник не подтвердил сделку, поэтому сумму нельзя выдавать за установленный факт."
    args = article_args() | {"needs_verification": True, "factuality_notes": notes}
    env.llm.responses = successful_responses()[:2] + [tool_call("save_article", args)]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.error == "facts_require_verification"
        rejected = job.agent_trace[-1]
        assert rejected["tool"] == "save_article"
        assert rejected["error"] == "facts_require_verification"
        assert rejected["detail"] == notes
        assert (await session.scalar(select(Publication))) is None
    assert env.ghost.post_calls == 0


async def test_validation_feedback_names_invalid_fields_without_echoing_article(env, queued):
    captured = []

    class InspectLLM(FakeLLM):
        async def complete(self, messages, tools):
            captured.extend(message["content"] for message in messages if message["role"] == "tool")
            return await super().complete(messages, tools)

    invalid = article_args() | {"summary": "private-generated-text-" * 20}
    env.llm = InspectLLM(
        successful_responses()[:2]
        + [
            tool_call("save_article", invalid),
            {"role": "assistant", "content": '{"outcome":"skipped","reason":"Needs correction"}'},
        ]
    )
    await worker(env).run_once()
    feedback = [json.loads(content) for content in captured if "invalid_tool_arguments" in content]
    assert feedback and feedback[-1]["validation_errors"][0]["field"] == "summary"
    assert "private-generated-text" not in json.dumps(feedback)
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        entry = next(item for item in job.agent_trace if item.get("validation_errors"))
        assert entry["validation_errors"][0]["field"] == "summary"
        assert "private-generated-text" not in json.dumps(job.agent_trace)


@pytest.mark.parametrize("demo_mode", [False, True])
async def test_factuality_gate_can_be_disabled_for_demo(env, queued, demo_mode):
    env.settings.demo_mode = demo_mode
    args = article_args() | {"needs_verification": True}
    env.llm.responses = successful_responses()[:2] + [
        tool_call("save_article", args),
        tool_call("publish_article"),
    ]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        publication = await session.scalar(select(Publication))
        if demo_mode:
            assert job.error == "auto_publish_disabled" and publication.status == "dry_run"
        else:
            assert job.error == "facts_require_verification" and publication is None
    assert env.ghost.post_calls == 0


async def test_demo_preserves_html_validation(env, queued):
    env.settings.demo_mode = True
    args = article_args() | {
        "needs_verification": True,
        "content_html": "<script>" + "x" * 150 + "</script>",
    }
    env.llm.responses = successful_responses()[:2] + [tool_call("save_article", args)]
    await worker(env).run_once()
    async with env.db.sessions() as session:
        job = await session.get(Job, queued.job_id)
        assert job.error == "article_body_too_short_after_sanitization"
    assert env.ghost.post_calls == 0
