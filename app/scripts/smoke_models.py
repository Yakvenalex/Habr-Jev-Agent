"""Opt-in dry run in an isolated temporary database; never posts to Ghost."""

import asyncio
import tempfile

from sqlalchemy import select

from app.clients.ghost import GhostClient
from app.clients.laya import LayaClient
from app.clients.llm import LLMClient
from app.config import Settings
from app.db.models import Job, Publication
from app.db.session import Database
from app.schemas.news import NewsInput
from app.services.ingestion import Ingestion
from app.worker import Worker


async def main():
    with tempfile.TemporaryDirectory() as temporary:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{temporary}/smoke.db", auto_publish=False
        )
        db = Database(settings.database_url)
        laya, llm, ghost = LayaClient(settings), LLMClient(settings), GhostClient(settings)
        await db.initialize()
        try:
            news = NewsInput.model_validate(
                {
                    "source": {
                        "type": "manual",
                        "name": "Python documentation",
                        "url": "https://docs.python.org/3.12/library/asyncio-task.html#task-groups",
                    },
                    "title": "Группы задач asyncio.TaskGroup в Python",
                    "content": "Документация Python 3.12 описывает asyncio.TaskGroup. "
                    "Класс добавлен в Python 3.11. Это асинхронный контекстный менеджер "
                    "для группы задач. Задачи создаются через TaskGroup.create_task. "
                    "При выходе из контекста ожидается завершение всех задач. "
                    "Если задача завершается с исключением, отличным от CancelledError, "
                    "остальные задачи группы отменяются. После завершения группы ошибки "
                    "объединяются в ExceptionGroup или BaseExceptionGroup. "
                    "KeyboardInterrupt и SystemExit после отмены остальных задач "
                    "пробрасываются отдельно. Источник не приводит бенчмарки.",
                }
            )
            result, code = await Ingestion(db, laya, settings).accept(news)
            print("Ingress:", code, result.status, flush=True)
            await Worker(db, settings, laya, llm, ghost).run_once()
            async with db.sessions() as session:
                job = await session.get(Job, result.job_id)
                publication = await session.scalar(select(Publication))
                print("Job:", job.status, "reason:", job.error, "steps:", job.current_step)
                print("Trace:", job.agent_trace)
                if publication:
                    print("Publication:", publication.status, "title:", publication.title)
                if not publication or publication.status != "dry_run":
                    raise SystemExit("Dry run did not produce a validated article")
        finally:
            await laya.close()
            await llm.close()
            await ghost.close()
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
