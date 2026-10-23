import json

import httpx
import jwt
import pytest
from pydantic import ValidationError

from app.clients.ghost import GhostClient
from app.clients.laya import Classification, LayaClient
from app.clients.llm import LLMClient
from app.config import Settings


async def test_laya_request_and_full_response():
    raw = {
        "answers": {
            "category": {
                "type": "choice",
                "choice": "technology",
                "probabilities": {
                    "technology": 0.97,
                    "business": 0.01,
                    "sports": 0.01,
                    "world": 0.01,
                },
                "confidence": 0.4,
            }
        },
        "diagnostics": {"example": True},
    }

    def handler(request):
        assert request.headers["X-Dev-Key"] == "dev-secret"
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        assert body["model"] == "multilingual"
        assert body["state"]["article"] == "input"
        assert body["questions"]["category"]["type"] == "choice"
        assert set(body["questions"]["category"]["criteria"]) == {
            "world",
            "sports",
            "business",
            "technology",
        }
        return httpx.Response(200, json=raw)

    settings = Settings(_env_file=None, dev_proxy_key="dev-secret")
    client = LayaClient(settings, transport=httpx.MockTransport(handler))
    result, response = await client.classify("input")
    assert result.choice == "technology" and response == raw
    await client.close()


@pytest.mark.parametrize(
    "probabilities",
    [
        {"technology": 0.5, "sports": 0.5},
        {"technology": 1.1, "sports": -0.1, "world": 0, "business": 0},
        {"technology": 0.1, "sports": 0.7, "world": 0.1, "business": 0.1},
        {"technology": 0.5, "sports": 0.1, "world": 0.1, "business": 0.1},
    ],
)
def test_invalid_laya_results(probabilities):
    with pytest.raises(ValidationError):
        Classification(type="choice", choice="technology", probabilities=probabilities)


async def test_laya_http_errors():
    client = LayaClient(
        Settings(_env_file=None),
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "unavailable"})),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.classify("test")
    await client.close()


async def test_llm_tool_request():
    def handler(request):
        assert request.headers["X-Dev-Key"] == "dev-secret"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        assert body["parallel_tool_calls"] is False
        assert body["tools"][0]["function"]["name"] == "get_source_context"
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "gemma4",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "get_source_context", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    from app.agent.registry import tool_definitions

    settings = Settings(_env_file=None, dev_proxy_key="dev-secret")
    client = LLMClient(
        settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    message = await client.complete([{"role": "user", "content": "test"}], tool_definitions())
    assert message["tool_calls"][0]["function"]["name"] == "get_source_context"
    await client.close()


async def test_ghost_jwt_and_publish():
    from types import SimpleNamespace

    secret = "ab" * 32
    requests = []

    def handler(request):
        token = request.headers["Authorization"].removeprefix("Ghost ")
        claims = jwt.decode(token, bytes.fromhex(secret), algorithms=["HS256"], audience="/admin/")
        assert jwt.get_unverified_header(token)["kid"] == "testid"
        assert claims["exp"] - claims["iat"] == 300
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"posts": []})
        body = json.loads(request.content)
        assert body["posts"][0]["status"] == "published"
        assert request.url.params["source"] == "html"
        return httpx.Response(
            201,
            json={
                "posts": [
                    {
                        "id": "postid",
                        "slug": "jev-test",
                        "url": "https://ghost.test/jev-test/",
                        "status": "published",
                    }
                ]
            },
        )

    client = GhostClient(
        Settings(
            _env_file=None, ghost_url="https://ghost.test", ghost_admin_api_key="testid:" + secret
        ),
        transport=httpx.MockTransport(handler),
    )
    assert await client.find("jev-test") is None
    result = await client.publish(
        SimpleNamespace(
            title="Title",
            slug="jev-test",
            content_html="<p>Test</p>",
            summary="Summary",
            tags=["Test"],
        )
    )
    assert result["id"] == "postid"
    assert all(r.url.host == "ghost.test" for r in requests)
    assert requests[0].url.path == "/ghost/api/admin/posts/"
    await client.close()


def test_settings_env(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ACCESS_TOKENS", raising=False)
    monkeypatch.delenv("DEV_PROXY_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "APP_ACCESS_TOKENS=one,two\nDEV_PROXY_KEY=secret\nLLM_BASE_URL=http://llm:8080/v1\n"
    )
    settings = Settings(_env_file=path)
    assert settings.access_tokens == ["one", "two"]
    assert settings.dev_headers == {"X-Dev-Key": "secret"}
    assert "secret" not in repr(settings)
