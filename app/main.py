"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.collectors.registry import build_default_adapter_registry
from app.config import Settings, get_settings
from app.logging import configure_logging
from app.security.browser import MutationRateLimiter, new_csrf_token
from app.security.headers import add_security_headers
from app.storage.database import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    database = Database(resolved_settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            database.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        debug=resolved_settings.debug,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.adapter_registry = build_default_adapter_registry()
    application.state.csrf_token = new_csrf_token()
    application.state.mutation_rate_limiter = MutationRateLimiter()
    application.middleware("http")(add_security_headers)
    application.include_router(api_router, prefix="/api/v1")
    from app.web.pages import router as web_router

    static_directory = Path(__file__).resolve().parent / "web" / "static"
    application.mount("/static", StaticFiles(directory=static_directory), name="static")
    application.include_router(web_router)

    return application


app = create_app()
