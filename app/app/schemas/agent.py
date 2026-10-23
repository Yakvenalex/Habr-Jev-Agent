from pydantic import BaseModel, ConfigDict, Field


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmptyArgs(ToolArgs):
    pass


class SimilarArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=2000)


class ArticleArgs(ToolArgs):
    title: str = Field(min_length=5, max_length=300)
    summary: str = Field(min_length=20, max_length=300)
    content_html: str = Field(min_length=100, max_length=30_000)
    tags: list[str] = Field(min_length=1, max_length=8)
    relevance_evidence: str = Field(min_length=20, max_length=2000)
    factuality_notes: str = Field(min_length=20, max_length=2000)
    needs_verification: bool


class AgentConclusion(ToolArgs):
    outcome: str = Field(pattern="^(skipped|completed)$")
    reason: str = Field(min_length=3, max_length=500)
