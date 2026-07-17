import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.config import Settings
from app.main import create_app


async def _get(application: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def test_health_is_versioned_and_non_sensitive() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        deepseek_api_key="test-only-secret-value",
    )
    response = asyncio.run(_get(create_app(settings), "/api/v1/health"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["environment"] == "test"
    assert payload["version"] == "0.1.0"
    assert "test-only-secret-value" not in response.text
    assert "database" not in response.text.lower()


def test_security_headers_are_added() -> None:
    application = create_app(Settings(_env_file=None, environment="test"))
    response = asyncio.run(_get(application, "/api/v1/health"))

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_application_exposes_no_transaction_routes() -> None:
    application = create_app(Settings(_env_file=None, environment="test"))
    prohibited_fragments = ("trade", "order", "redeem", "purchase", "transaction")

    paths = {
        route.path.lower()
        for route in application.routes
        if isinstance(getattr(route, "path", None), str)
    }
    for path in paths:
        assert not any(fragment in path for fragment in prohibited_fragments)
