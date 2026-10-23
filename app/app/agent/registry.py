from dataclasses import dataclass

from app.agent.tools.articles import save_article
from app.agent.tools.duplicates import find_similar_news
from app.agent.tools.publishing import publish_article
from app.agent.tools.sources import get_source_context
from app.schemas.agent import ArticleArgs, EmptyArgs, SimilarArgs


@dataclass
class ToolContext:
    db: object
    settings: object
    state: object
    laya: object
    ghost: object


TOOLS = {
    "get_source_context": (EmptyArgs, get_source_context, "Получить исходную новость и метаданные"),
    "find_similar_news": (SimilarArgs, find_similar_news, "Найти похожие обработанные новости"),
    "save_article": (ArticleArgs, save_article, "Проверить и сохранить оригинальную статью"),
    "publish_article": (EmptyArgs, publish_article, "Опубликовать сохранённую проверенную статью"),
}


def tool_definitions(publish_requested=False):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema.model_json_schema(),
            },
        }
        for name, (schema, _, description) in TOOLS.items()
        if not (publish_requested and name == "save_article")
    ]
