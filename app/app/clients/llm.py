from openai import AsyncOpenAI

from app.config import Settings


class LLMClient:
    def __init__(self, settings: Settings, http_client=None):
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            default_headers=settings.dev_headers,
            timeout=settings.external_timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, messages: list, tools: list):
        response = await self.client.chat.completions.create(
            model=self.settings.llm_model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            max_tokens=self.settings.agent_max_output_tokens,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": self.settings.llm_enable_thinking,
                }
            },
        )
        return response.choices[0].message.model_dump(exclude_none=True)

    async def ready(self):
        models = await self.client.models.list()
        return any(model.id == self.settings.llm_model for model in models.data)

    async def close(self):
        await self.client.close()
