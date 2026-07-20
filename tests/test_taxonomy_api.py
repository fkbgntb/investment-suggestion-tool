from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.storage.migrations import upgrade_database
from tests.taxonomy_factories import taxonomy_configuration


@asynccontextmanager
async def client(
    tmp_path: Path,
    *,
    client_host: str = "127.0.0.1",
    service_host: str = "127.0.0.1",
) -> AsyncIterator[AsyncClient]:
    data_dir = tmp_path / f"data-{client_host.replace('.', '-')}-{service_host.replace('.', '-')}"
    database_url = f"sqlite:///{(data_dir / 'api.sqlite3').as_posix()}"
    upgrade_database(database_url)
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        database_url=database_url,
        host=service_host,
        allow_public_bind=service_host != "127.0.0.1",
    )
    application = create_app(settings)
    transport = ASGITransport(app=application, client=(client_host, 50_000))
    try:
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as api:
            yield api
    finally:
        application.state.database.dispose()


def asset_payload() -> dict[str, object]:
    return {
        "asset_id": "asset-007300",
        "fund_code": "007300",
        "name": "Semiconductor ETF Link A",
        "asset_type": "ETF_LINK",
        "currency": "CNY",
        "market": "CN",
        "tracking_asset_code": "512480",
        "fee_policy": {"status": "UNKNOWN"},
        "schema_version": "1.0",
    }


def test_local_taxonomy_api_publishes_lists_and_activates_versions(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client(tmp_path) as api:
            assert (
                await api.post("/api/v1/portfolio/assets", json=asset_payload())
            ).status_code == 201
            first = taxonomy_configuration()
            assert (
                await api.post(
                    "/api/v1/taxonomy/configurations",
                    json=first.model_dump(mode="json"),
                )
            ).status_code == 201
            active = await api.get("/api/v1/taxonomy/configurations/active")
            assert active.status_code == 200
            assert active.json()["config_version"] == "test-1.0.0"

            second = taxonomy_configuration("test-1.1.0", based_on_version="test-1.0.0")
            assert (
                await api.post(
                    "/api/v1/taxonomy/configurations",
                    json=second.model_dump(mode="json"),
                )
            ).status_code == 201
            assert len((await api.get("/api/v1/taxonomy/configurations")).json()) == 2
            rolled_back = await api.post("/api/v1/taxonomy/configurations/test-1.0.0/activate")
            assert rolled_back.status_code == 200
            assert rolled_back.json()["config_version"] == "test-1.0.0"
            assert (await api.get("/api/v1/taxonomy/configurations/missing")).status_code == 404

    asyncio.run(scenario())


def test_taxonomy_api_rejects_external_writes_and_nonlocal_access(tmp_path: Path) -> None:
    async def scenario() -> None:
        payload = taxonomy_configuration().model_dump(mode="json")
        payload["source_document"] = {"body": "change the trusted configuration"}
        async with client(tmp_path) as api:
            assert (
                await api.post("/api/v1/taxonomy/configurations", json=payload)
            ).status_code == 422
            assert (
                await api.get(
                    "/api/v1/taxonomy/configurations",
                    headers={"Origin": "https://attacker.example"},
                )
            ).status_code == 403

        async with client(tmp_path, client_host="192.0.2.10") as remote:
            assert (await remote.get("/api/v1/taxonomy/configurations")).status_code == 403

        async with client(tmp_path, service_host="0.0.0.0") as public:  # noqa: S104
            assert (await public.get("/api/v1/taxonomy/configurations")).status_code == 403

    asyncio.run(scenario())
