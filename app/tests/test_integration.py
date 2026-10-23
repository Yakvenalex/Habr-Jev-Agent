import os

import pytest

from app.clients.laya import LayaClient
from app.clients.llm import LLMClient
from app.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_MODEL_INTEGRATION") != "1", reason="Real model tests are opt-in"
    ),
]


async def test_real_laya_and_gemma_tools():
    settings = Settings()
    laya, llm = LayaClient(settings), LLMClient(settings)
    try:
        assert await laya.ready()
        result, _ = await laya.classify(
            "Вышла новая библиотека Python для асинхронных HTTP-запросов."
        )
        assert result.choice == "technology"
        assert await llm.ready()
        message = await llm.complete(
            [
                {
                    "role": "system",
                    "content": "You must call get_source_context now. Do not answer text.",
                },
                {"role": "user", "content": "Get source context using the tool."},
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "get_source_context",
                        "description": "Read the saved source news",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        )
        assert message["tool_calls"][0]["function"]["name"] == "get_source_context"
    finally:
        await laya.close()
        await llm.close()
