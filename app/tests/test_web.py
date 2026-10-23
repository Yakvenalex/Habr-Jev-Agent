from datetime import UTC, datetime, timedelta

import jwt
import pytest
from conftest import successful_responses
from pydantic import SecretStr

from app.api.web_auth import COOKIE_NAME
from app.worker import Worker


@pytest.fixture
async def signed_in(client, env):
    env.settings.web_password = SecretStr("test-password")
    env.settings.web_session_secret = SecretStr("s" * 48)
    client.headers.pop("Authorization", None)
    result = await client.post(
        "/auth/login", json={"username": "admin", "password": "test-password"}
    )
    assert result.status_code == 200
    return client


async def test_login_redirects_and_public_assets(client):
    client.headers.pop("Authorization")
    for path in ["/", "/docs"]:
        response = await client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    assert (await client.get("/login")).status_code == 200
    assert (await client.get("/static/dashboard.js")).status_code == 200
    assert (await client.get("/v1/news")).status_code == 401
    assert (await client.get("/openapi.json")).status_code == 401


async def test_login_cookie_and_refresh(signed_in):
    for _ in range(2):
        response = await signed_in.get("/")
        assert response.status_code == 200 and "Из новости" in response.text
        assert "test-password" not in response.text and "s" * 48 not in response.text
    assert (await signed_in.get("/login")).status_code == 303
    assert (await signed_in.get("/docs")).status_code == 200
    schema = (await signed_in.get("/openapi.json")).json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/v1/news"]["post"]["security"]
    assert (await signed_in.get("/v1/news")).status_code == 200


async def test_cookie_properties(signed_in):
    result = await signed_in.post(
        "/auth/login", json={"username": "admin", "password": "test-password"}
    )
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=43200" in cookie
    assert "test-password" not in cookie


async def test_wrong_password_and_logout(signed_in):
    result = await signed_in.post(
        "/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert result.status_code == 401
    assert (await signed_in.post("/auth/logout")).status_code == 200
    assert (await signed_in.get("/")).status_code == 303
    assert (await signed_in.get("/v1/news")).status_code == 401


async def test_cross_origin_cookie_writes_rejected(signed_in, payload):
    for path, data in [
        ("/v1/news", payload),
        ("/auth/logout", {}),
        ("/auth/login", {"username": "admin", "password": "test-password"}),
    ]:
        result = await signed_in.post(path, json=data, headers={"Origin": "https://evil.invalid"})
        assert result.status_code == 403
    assert (
        await signed_in.post("/v1/news", json=payload, headers={"Origin": "http://test"})
    ).status_code == 202


@pytest.mark.parametrize("token", ["tampered", "expired", "wrong-audience"])
async def test_bad_sessions_rejected(signed_in, env, token):
    if token == "tampered":
        value = "invalid.token.value"
    else:
        value = jwt.encode(
            {
                "sub": "admin",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(seconds=-60 if token == "expired" else 60),
                "aud": "other" if token == "wrong-audience" else "jev-web",
            },
            env.settings.web_session_secret.get_secret_value(),
            algorithm="HS256",
        )
    signed_in.cookies.clear()
    signed_in.cookies.set(COOKIE_NAME, value)
    assert (await signed_in.get("/v1/news")).status_code == 401


async def test_article_preview_and_history(signed_in, env, payload):
    result = (await signed_in.post("/v1/news", json=payload)).json()
    env.llm.responses = successful_responses()
    await Worker(env.db, env.settings, env.laya, env.llm, env.ghost).run_once()
    history = (await signed_in.get("/v1/news")).json()["items"]
    assert history[0]["id"] == result["id"] and history[0]["job_id"] == result["job_id"]
    article = (await signed_in.get("/v1/news/" + result["id"] + "/article")).json()
    assert article["status"] == "dry_run" and "<p>" in article["content_html"]
    job = (await signed_in.get("/v1/jobs/" + result["job_id"])).json()
    assert any(entry.get("tool") == "save_article" for entry in job["agent_trace"])


async def test_assets_use_same_origin_paths(signed_in):
    response = await signed_in.get("/")
    assert 'href="/static/dashboard.css"' in response.text
    assert 'src="/static/dashboard.js"' in response.text
    assert "http://test/static/" not in response.text
    await signed_in.post("/auth/logout")
    login = await signed_in.get("/login")
    assert 'href="/static/dashboard.css"' in login.text
    assert 'src="/static/login.js"' in login.text


@pytest.mark.parametrize("trusted", [True, False])
async def test_https_login_behind_trusted_proxy(env, trusted):
    import httpx
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import create_app

    env.settings.web_password = SecretStr("test-password")
    env.settings.web_session_secret = SecretStr("s" * 48)
    application = create_app(env.settings, db=env.db, laya=env.laya, llm=env.llm, ghost=env.ghost)
    wrapped = ProxyHeadersMiddleware(application, trusted_hosts="172.18.0.1")
    peer = "172.18.0.1" if trusted else "203.0.113.5"
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped, client=(peer, 1234)),
            base_url="http://example.com",
        ) as client:
            response = await client.post(
                "/auth/login",
                json={"username": "admin", "password": "test-password"},
                headers={"Origin": "https://example.com", "X-Forwarded-Proto": "https"},
            )
            assert response.status_code == (200 if trusted else 403)
            if trusted:
                assert "Secure" in response.headers["set-cookie"]
