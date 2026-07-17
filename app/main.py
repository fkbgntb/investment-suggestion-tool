"""FastAPI application factory."""

from fastapi import FastAPI

from app.api.router import api_router
from app.config import Settings, get_settings
from app.logging import configure_logging
from app.security.headers import add_security_headers


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        debug=resolved_settings.debug,
    )
    application.state.settings = resolved_settings
    application.middleware("http")(add_security_headers)
    application.include_router(api_router, prefix="/api/v1")

    @application.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": resolved_settings.app_name,
            "version": resolved_settings.app_version,
            "health": "/api/v1/health",
            "docs": "/docs",
        }

    return application


app = create_app()
