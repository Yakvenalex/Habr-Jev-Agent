import asyncio
import json

import pytest
from conftest import successful_responses, tool_call
from sqlalchemy import func, select, text

from app.db.models import Job, Publication, utcnow
from app.db.session import Database
from app.worker import Worker


def worker(env, db=None):
    return Worker(db or env.db, env.settings, env.laya, env.llm, env.ghost)


def publish_responses():
    return [
        tool_call("get_source_context"),
        tool_call("find_similar_news", {"query": "Python asyncio HTTP"}),
        tool_call("publish_article"),
    ]


@pytest.fixture
async def draft(env, queued):
    env.llm.responses = successful_responses()
    await worker(env).run_once()
    return queued


async def test_explicit_request_publishes_exact_draft_despite_auto_publish_false(
    client, env, draft
):
    before = (await client.get(f"/v1/news/{draft.id}/article")).json()
    assert before["can_publish"] and env.settings.auto_publish is False
    response = await client.post(f"/v1/news/{draft.id}/publish")
    assert response.status_code == 202 and response.json()["job_id"] == draft.job_id
    env.llm.responses = publish_responses()
    await worker(env).run_once()
    article = (await client.get(f"/v1/news/{draft.id}/article")).json()
    job = (await client.get(f"/v1/jobs/{draft.job_id}")).json()
    assert job["status"] == "published" and job["publish_requested"] is True
    assert article["status"] == "published" and not article["can_publish"]
    assert article["content_html"] == before["content_html"]
    assert article["title"] == before["title"]
    assert article["ghost_url"] and env.ghost.post_calls == 1
    assert env.llm.calls == 7  # four preparation steps, three publication review steps
    assert (await client.post(f"/v1/news/{draft.id}/publish")).status_code == 200
    assert env.ghost.post_calls == 1


async def test_concurrent_publication_requests_share_one_job(client, env, draft):
    responses = await asyncio.gather(
        *[client.post(f"/v1/news/{draft.id}/publish") for _ in range(4)]
    )
    assert all(response.status_code == 202 for response in responses)
    assert {response.json()["job_id"] for response in responses} == {draft.job_id}
    async with env.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        job = await session.get(Job, draft.job_id)
        assert sum(event.get("event") == "publish_requested" for event in job.agent_trace) == 1


async def test_unvalidated_and_uncertain_drafts_cannot_be_published(client, env, draft):
    for status in ["draft", "submitting"]:
        async with env.db.sessions() as session:
            publication = await session.scalar(select(Publication))
            publication.status = status
            await session.commit()
        assert (await client.post(f"/v1/news/{draft.id}/publish")).status_code == 409
    assert env.ghost.post_calls == 0


async def test_publish_requires_auth(client, env, draft):
    client.headers.pop("Authorization")
    assert (await client.post(f"/v1/news/{draft.id}/publish")).status_code == 401
    assert env.ghost.post_calls == 0


async def test_manual_agent_may_reject_draft(client, env, draft):
    await client.post(f"/v1/news/{draft.id}/publish")
    env.llm.responses = [
        {
            "role": "assistant",
            "content": json.dumps({"outcome": "skipped", "reason": "Факты требуют проверки"}),
        }
    ]
    await worker(env).run_once()
    job = (await client.get(f"/v1/jobs/{draft.job_id}")).json()
    assert job["status"] == "skipped" and job["error"] == "Факты требуют проверки"
    assert env.ghost.post_calls == 0


async def test_manual_review_tools_are_read_only(client, env, draft):
    from app.agent.registry import tool_definitions

    assert "save_article" not in {entry["function"]["name"] for entry in tool_definitions(True)}
    before = (await client.get(f"/v1/news/{draft.id}/article")).json()
    await client.post(f"/v1/news/{draft.id}/publish")
    env.llm.responses = [tool_call("save_article", {})] + publish_responses()
    await worker(env).run_once()
    after = (await client.get(f"/v1/news/{draft.id}/article")).json()
    assert before["content_html"] == after["content_html"]
    job = (await client.get(f"/v1/jobs/{draft.job_id}")).json()
    assert any(
        item.get("tool") == "save_article" and item.get("error") == "unknown_tool"
        for item in job["agent_trace"]
    )


async def test_manual_rechecks_generated_topic(client, env, draft):
    await client.post(f"/v1/news/{draft.id}/publish")
    env.laya.category = "sports"
    env.llm.responses = publish_responses()
    await worker(env).run_once()
    job = (await client.get(f"/v1/jobs/{draft.job_id}")).json()
    assert job["error"] == "generated_article_not_technology" and env.ghost.post_calls == 0


async def test_manual_obeys_rate_limit(client, env, draft):
    env.settings.max_posts_per_hour = 1
    async with env.db.sessions() as session:
        publication = await session.scalar(select(Publication))
        publication.submission_at = utcnow()
        await session.commit()
    await client.post(f"/v1/news/{draft.id}/publish")
    env.llm.responses = publish_responses()
    await worker(env).run_once()
    job = (await client.get(f"/v1/jobs/{draft.job_id}")).json()
    assert job["error"] == "hourly_publication_limit" and env.ghost.post_calls == 0


async def test_manual_request_survives_restarting_worker(client, env, draft):
    await client.post(f"/v1/news/{draft.id}/publish")
    second = Database(env.settings.database_url)
    await second.initialize()
    try:
        env.llm.responses = publish_responses()
        await worker(env, second).run_once()
        assert (await client.get(f"/v1/jobs/{draft.job_id}")).json()["status"] == "published"
    finally:
        await second.close()


async def test_existing_database_upgrades_without_losing_drafts(env, draft):
    async with env.db.sessions() as session:
        await session.execute(text("ALTER TABLE jobs DROP COLUMN publish_requested"))
        await session.commit()
    await env.db.initialize()
    await env.db.initialize()  # upgrade is idempotent
    async with env.db.sessions() as session:
        job = await session.get(Job, draft.job_id)
        article = await session.scalar(select(Publication))
        assert job.publish_requested is False and article.status == "dry_run"
