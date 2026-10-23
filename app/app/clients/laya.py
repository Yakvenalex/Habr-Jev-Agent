import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings


class InvalidLayaResponse(ValueError):
    def __init__(self, raw):
        super().__init__("Invalid Laya response schema")
        self.raw = raw


class Classification(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    choice: str
    probabilities: dict[str, float] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def valid_probabilities(self):
        expected = {"world", "sports", "business", "technology"}
        if self.type != "choice" or set(self.probabilities) != expected:
            raise ValueError("Unexpected Laya category schema")
        if self.choice not in expected:
            raise ValueError("Unknown category")
        if any(not 0 <= p <= 1 for p in self.probabilities.values()):
            raise ValueError("Invalid probability")
        if abs(sum(self.probabilities.values()) - 1) > 0.02:
            raise ValueError("Probabilities do not sum to one")
        if self.probabilities[self.choice] != max(self.probabilities.values()):
            raise ValueError("Choice differs from maximum probability")
        return self


class LayaClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.laya_base_url,
            headers=settings.dev_headers,
            timeout=settings.external_timeout_seconds,
            transport=transport,
        )

    async def classify(self, content: str) -> tuple[Classification, dict]:
        response = await self.http.post(
            "/v1/systemone",
            json={
                "model": self.settings.laya_model,
                "state": {"article": content},
                "questions": {
                    "category": {
                        "type": "choice",
                        "instructions": self.settings.laya_instructions,
                        "criteria": self.settings.laya_categories,
                    }
                },
            },
        )
        response.raise_for_status()
        raw = response.json()
        try:
            result = Classification.model_validate(raw["answers"]["category"])
        except (ValueError, TypeError, KeyError):
            raise InvalidLayaResponse(raw) from None
        return result, raw

    async def ready(self):
        response = await self.http.get("/health")
        response.raise_for_status()
        body = response.json()
        return body.get("status") == "ok" and self.settings.laya_model in body.get("loaded", [])

    async def close(self):
        await self.http.aclose()
