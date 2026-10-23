import asyncio
import copy

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db.models import Job, News
from app.schemas.news import NewsInput
from app.services.ingestion import Ingestion


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic test-token", "Bearer"])
async def test_auth(client, header):
    client.headers.pop("Authorization", None)
    headers = {"Authorization": header} if header else {}
    for path in [
        "/ready",
        "/v1/news/00000000-0000-0000-0000-000000000001",
        "/v1/jobs/00000000-0000-0000-0000-000000000001",
    ]:
        assert (await client.get(path, headers=headers)).status_code == 401
    assert (await client.post("/v1/news", json={}, headers=headers)).status_code == 401
    assert (await client.get("/health")).status_code == 200


async def test_query_token_rejected(client):
    client.headers.pop("Authorization")
    assert (await client.get("/ready?token=test-token")).status_code == 401


async def test_second_token_and_ready(client):
    response = await client.get("/ready", headers={"Authorization": "Bearer second-token"})
    assert response.status_code == 200
    assert all(response.json()["checks"].values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", ""),
        ("content", "  \n "),
        ("content", "a" * 20001),
        ("published_at", "not-date"),
        ("published_at", "2026-10-23T10:00:00"),
        ("source", {"type": "unknown", "name": "name"}),
        ("source", {"type": "manual", "name": "  "}),
        ("source", {"type": "manual", "name": "name", "url": "file:///etc/passwd"}),
        ("source", {"type": "manual", "name": "name", "url": "https://user:pass@example.com"}),
    ],
)
async def test_invalid_input(client, payload, field, value):
    payload[field] = value
    assert (await client.post("/v1/news", json=payload)).status_code == 422


async def test_queue_and_lookup(client, env, payload):
    response = await client.post("/v1/news", json=payload)
    assert response.status_code == 202
    result = response.json()
    assert result["agent_started"] is True
    assert (await client.get("/v1/jobs/" + result["job_id"])).json()["status"] == "queued"
    news = (await client.get("/v1/news/" + result["id"])).json()
    assert news["category"] == "technology"
    async with env.db.sessions() as session:
        row = await session.get(News, result["id"])
        assert row.laya_response["answers"]["category"]["choice"] == "technology"


@pytest.mark.parametrize("duplicate", ["content", "url", "external_id"])
async def test_exact_duplicates_before_laya(client, env, payload, duplicate):
    first = (await client.post("/v1/news", json=payload)).json()
    second = copy.deepcopy(payload)
    second["content"] = "Совсем другой текст"
    second["source"]["url"] = "https://example.com/news/2"
    second["source"]["external_id"] = "2"
    if duplicate == "content":
        second["content"] = "  " + payload["content"].upper().replace(" ", "  ")
    elif duplicate == "url":
        second["source"]["url"] = payload["source"]["url"] + "?utm_source=test#fragment"
    else:
        second["source"]["external_id"] = " 1 "
    response = await client.post("/v1/news", json=second)
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert response.json()["id"] == first["id"]
    assert env.laya.calls == 1


async def test_concurrent_duplicate(env, payload):
    service = Ingestion(env.db, env.laya, env.settings)
    results = await asyncio.gather(
        *[service.accept(NewsInput.model_validate(payload)) for _ in range(4)]
    )
    assert sum(r.status == "duplicate" for r, _ in results) == 3
    assert env.laya.calls == 1
    async with env.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(News)) == 1
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_resubmit_missing_source_restores_job_once(client, env, payload):
    from conftest import successful_responses

    from app.worker import Worker

    without_url = copy.deepcopy(payload)
    del without_url["source"]["url"]
    first = (await client.post("/v1/news", json=without_url)).json()
    env.llm.responses = successful_responses()
    worker = Worker(env.db, env.settings, env.laya, env.llm, env.ghost)
    await worker.run_once()
    assert (await client.get("/v1/jobs/" + first["job_id"])).json()["error"] == (
        "missing_identifiable_source_url"
    )

    responses = await asyncio.gather(*[client.post("/v1/news", json=payload) for _ in range(3)])
    assert sorted(r.status_code for r in responses) == [200, 200, 202]
    repaired = next(r.json() for r in responses if r.status_code == 202)
    assert repaired["id"] == first["id"] and repaired["job_id"] == first["job_id"]
    assert repaired["reason"] == "source_url_restored"
    env.llm.responses = successful_responses()
    await worker.run_once()
    article = (await client.get("/v1/news/" + first["id"] + "/article")).json()
    assert article["status"] == "dry_run"
    assert article["source_url"] == payload["source"]["url"]
    assert payload["source"]["url"] in article["content_html"]
    async with env.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(News)) == 1
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.parametrize("change", ["active", "different_content", "conflicting_url"])
async def test_source_repair_does_not_modify_other_work(client, env, payload, change):
    without_url = copy.deepcopy(payload)
    del without_url["source"]["url"]
    first = (await client.post("/v1/news", json=without_url)).json()
    async with env.db.sessions() as session:
        news = await session.get(News, first["id"])
        job = await session.get(Job, first["job_id"])
        news.status = job.status = "skipped"
        news.reason = job.error = "missing_identifiable_source_url"
        if change == "active":
            job.status = "processing"
        await session.commit()
    if change == "different_content":
        payload["content"] = "Другой исходный материал"
    elif change == "conflicting_url":
        other = copy.deepcopy(payload)
        other["content"] = "Другой текст новости"
        other["source"]["external_id"] = "other"
        assert (await client.post("/v1/news", json=other)).status_code == 202
    response = await client.post("/v1/news", json=payload)
    assert response.status_code == 200
    async with env.db.sessions() as session:
        assert (await session.get(News, first["id"])).source_url is None


@pytest.mark.parametrize(
    ("category", "probabilities", "expected"),
    [
        ("sports", None, "skipped"),
        ("world", None, "skipped"),
        ("business", None, "queued"),
        ("world", {"world": 0.40, "technology": 0.35, "business": 0.20, "sports": 0.05}, "queued"),
    ],
)
async def test_routing(client, env, payload, category, probabilities, expected):
    env.laya.category, env.laya.probabilities = category, probabilities
    response = await client.post("/v1/news", json=payload)
    assert response.json()["status"] == expected
    assert response.json()["agent_started"] == (expected == "queued")
    assert env.llm.calls == 0


async def test_laya_failure_is_durable(client, env, payload):
    env.laya.error = httpx.ReadTimeout("secret response")
    response = await client.post("/v1/news", json=payload)
    assert response.status_code == 202
    assert response.json()["reason"] == "classification_retry"
    assert response.json()["agent_started"] is False
    async with env.db.sessions() as session:
        assert (await session.get(Job, response.json()["job_id"])).status == "queued"


async def test_sqlite_settings(env):
    async with env.db.sessions() as session:
        assert await session.scalar(text("PRAGMA journal_mode")) == "wal"
        assert await session.scalar(text("PRAGMA busy_timeout")) == 10000
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1


async def test_readiness_reports_dependency_failure(client, env):
    async def unavailable():
        raise httpx.ConnectError("upstream secret details")

    env.laya.ready = unavailable
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["laya"] is False
    assert "secret" not in response.text


async def test_malformed_laya_json_retained_for_diagnostics(client, env, payload):
    from app.clients.laya import InvalidLayaResponse

    raw = {"answers": {"category": {"choice": "unexpected"}}}
    env.laya.error = InvalidLayaResponse(raw)
    response = await client.post("/v1/news", json=payload)
    assert response.status_code == 202
    async with env.db.sessions() as session:
        assert (await session.get(News, response.json()["id"])).laya_response == raw
