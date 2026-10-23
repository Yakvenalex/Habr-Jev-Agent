from dataclasses import dataclass, field


class LeaseLost(Exception):
    pass


class RejectArticle(Exception):
    """A fail-closed publication policy decision."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.detail = detail


class PublicationUncertain(Exception):
    """Reconcile by slug, never blindly repeat a potentially successful POST."""


@dataclass
class AgentState:
    job_id: str
    news_id: str
    lease_token: str
    source_checked: bool = False
    duplicates_checked: bool = False
    trace: list = field(default_factory=list)
    publish_requested: bool = False
