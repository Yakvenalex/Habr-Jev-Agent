import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.api import jobs, news
from app.api.auth import authorize
from app.api.web_auth import current_user
from app.api.web_auth import router as auth_router
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
        title="Habr-Jev-Agent",
        description="Laya классифицирует новость, Gemma готовит статью, Ghost публикует результат. "
        "В браузере используется текущая сессия входа. Для внешнего клиента "
        "нажми Authorize и вставь токен из APP_ACCESS_TOKENS.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True},
    )
    web_root = Path(__file__).parent / "web"
    templates = Jinja2Templates(directory=web_root / "templates")
    app.mount("/static", StaticFiles(directory=web_root / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "auto_publish": settings.auto_publish,
                "max_steps": settings.agent_max_steps,
                "max_posts": settings.max_posts_per_hour,
                "username": current_user(request),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        if current_user(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request, name="login.html", context={}, headers={"Cache-Control": "no-store"}
        )

    @app.get("/docs", include_in_schema=False)
    async def swagger(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", status_code=303)
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="JEV · Swagger API",
            swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True},
        )

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(authorize)])
    async def openapi_schema():
        return JSONResponse(app.openapi(), headers={"Cache-Control": "no-store"})

    app.include_router(auth_router)
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
    uvicorn.run(
        create_app(settings),
        host=settings.app_host,
        port=settings.app_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )


if __name__ == "__main__":
    run()
