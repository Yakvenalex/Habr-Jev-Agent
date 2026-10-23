import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import jobs, news
from app.api.auth import authorize
from app.clients.laya import LayaClient
from app.config import Settings
from app.db.session import Database


def create_app(settings: Settings | None = None, *, db=None, laya=None, llm=None, ghost=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        from app.clients.ghost import GhostClient
        from app.clients.llm import LLMClient

        app.state.settings = settings
        app.state.db = db or Database(settings.database_url)
        app.state.laya = laya or LayaClient(settings)
        app.state.llm = llm or LLMClient(settings)
        app.state.ghost = ghost or GhostClient(settings)
        await app.state.db.initialize()
        try:
            yield
        finally:
            await app.state.laya.close()
            await app.state.llm.close()
            await app.state.ghost.close()
            await app.state.db.close()

    app = FastAPI(
        title="Habr-Jev-Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.include_router(news.router)
    app.include_router(jobs.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready", dependencies=[Depends(authorize)])
    async def ready():
        async def database_ready():
            async with app.state.db.sessions() as session:
                await session.execute(text("SELECT 1"))
            return True

        checks = {
            "database": database_ready(),
            "laya": app.state.laya.ready(),
            "llm": app.state.llm.ready(),
        }
        if settings.auto_publish:
            checks["ghost"] = app.state.ghost.ready()
        results = await asyncio.gather(*checks.values(), return_exceptions=True)
        statuses = {key: result is True for key, result in zip(checks, results, strict=True)}
        ok = all(statuses.values())
        return JSONResponse(
            {"status": "ok" if ok else "unavailable", "checks": statuses},
            status_code=200 if ok else 503,
        )

    return app


def run():
    import uvicorn

    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    run()
