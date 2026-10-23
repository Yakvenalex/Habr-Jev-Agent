from app.clients.laya import Classification
from app.config import Settings


def should_queue(result: Classification, settings: Settings) -> bool:
    if result.choice in {"technology", "business"}:
        return True
    ranked = sorted(result.probabilities.values(), reverse=True)
    return not (
        ranked[0] >= settings.routing_min_probability
        and ranked[0] - ranked[1] >= settings.routing_min_margin
    )
