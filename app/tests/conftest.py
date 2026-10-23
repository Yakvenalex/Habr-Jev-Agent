import json
from types import SimpleNamespace

import httpx
import pytest

from app.clients.laya import Classification
from app.config import Settings
from app.db.session import Database
from app.main import create_app
from app.schemas.news import NewsInput
from app.services.ingestion import Ingestion


class FakeLaya:
    def __init__(self, category="technology", probabilities=None):
        self.category = category
        self.probabilities = probabilities
        self.calls = 0
        self.error = None

    async def classify(self, _text):
        self.calls += 1
        if self.error:
            raise self.error
        probabilities = self.probabilities or {
            c: 0.97 if c == self.category else 0.01
            for c in ["technology", "business", "world", "sports"]
        }
        raw = {
            "answers": {
                "category": {
                    "type": "choice",
                    "choice": self.category,
                    "probabilities": probabilities,
                }
            }
        }
        return Classification.model_validate(raw["answers"]["category"]), raw

    async def ready(self):
        return True

    async def close(self):
        pass


class FakeLLM:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def ready(self):
        return True

    async def close(self):
        pass


class FakeGhost:
    def __init__(self):
        self.posts = {}
        self.post_calls = 0
        self.timeout_after_create = False
        self.timeout_before_create = False

    def headers(self):
        return {}

    async def find(self, slug):
        return self.posts.get(slug)

    async def publish(self, article):
        self.post_calls += 1
        if self.timeout_before_create:
            raise httpx.ReadTimeout("sensitive upstream message")
        post = {
            "id": "ghost-id",
            "url": "https://test.invalid/" + article.slug,
            "slug": article.slug,
            "status": "published",
        }
        self.posts[article.slug] = post
        if self.timeout_after_create:
            raise httpx.ReadTimeout("sensitive upstream message")
        return post

    async def ready(self):
        return True

    async def close(self):
        pass


@pytest.fixture
def payload():
    return {
        "source": {
            "type": "manual",
            "name": "Test source",
            "external_id": "1",
            "url": "https://example.com/news/1",
        },
        "title": "Новая библиотека Python для HTTP",
        "content": "Разработчики представили библиотеку Python для асинхронных HTTP-запросов. "
        "В исходном сообщении описана поддержка asyncio. Результаты измерений "
        "производительности не опубликованы.",
        "language": "ru",
        "published_at": "2026-10-23T09:00:00Z",
    }


@pytest.fixture
async def env(tmp_path):
    settings = Settings(
        _env_file=None,
        app_access_tokens="test-token,second-token",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        auto_publish=False,
    )
    db = Database(settings.database_url)
    await db.initialize()
    environment = SimpleNamespace(
        settings=settings, db=db, laya=FakeLaya(), llm=FakeLLM(), ghost=FakeGhost()
    )
    yield environment
    await db.close()


@pytest.fixture
async def client(env):
    app = create_app(env.settings, db=env.db, laya=env.laya, llm=env.llm, ghost=env.ghost)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            yield client


@pytest.fixture
async def queued(env, payload):
    result, code = await Ingestion(env.db, env.laya, env.settings).accept(
        NewsInput.model_validate(payload)
    )
    assert code == 202
    return result


def tool_call(name, args=None):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": name,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args or {}, ensure_ascii=False),
                },
            }
        ],
    }


def article_args():
    return {
        "title": "Новая библиотека Python поддерживает asyncio",
        "summary": "В сообщении разработчиков описана библиотека для HTTP-запросов и asyncio.",
        "content_html": "<p>По сообщению источника, разработчики представили библиотеку Python "
        "для асинхронных HTTP-запросов с поддержкой asyncio.</p>"
        "<p>Исходное сообщение не содержит результатов измерений "
        "производительности, поэтому оценить ускорение по нему нельзя.</p>",
        "tags": ["Python", "asyncio"],
        "relevance_evidence": "Разработка Python-библиотеки для асинхронных HTTP-запросов.",
        "factuality_notes": "Все утверждения атрибутированы исходному сообщению. Цифр и "
        "неподтверждённых результатов измерений нет.",
        "needs_verification": False,
    }


def successful_responses():
    return [
        tool_call("get_source_context"),
        tool_call("find_similar_news", {"query": "Новая библиотека Python для HTTP"}),
        tool_call("save_article", article_args()),
        tool_call("publish_article"),
    ]
