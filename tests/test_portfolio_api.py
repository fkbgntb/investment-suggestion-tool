from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.storage.migrations import upgrade_database
from tests.domain_factories import NOW, OPENED_ON, investment_profile, money


@asynccontextmanager
async def client(
    tmp_path: Path,
    *,
    client_host: str = "127.0.0.1",
    service_host: str = "127.0.0.1",
) -> AsyncIterator[AsyncClient]:
    data_dir = tmp_path / "data"
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


def profile_payload() -> dict[str, object]:
    return investment_profile().model_dump(mode="json")


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


def position_payload() -> dict[str, object]:
    return {
        "position_id": "position-007300",
        "profile_id": "profile-demo",
        "asset_id": "asset-007300",
        "units": "157.89",
        "cost_basis": money("640").model_dump(mode="json"),
        "current_value": money("653.59").model_dump(mode="json"),
        "average_cost_per_unit": "4.0535",
        "opened_on": OPENED_ON.isoformat(),
        "latest_purchase_on": OPENED_ON.replace(month=7).isoformat(),
        "recurring_contribution": money("50").model_dump(mode="json"),
        "purchase_lots": [],
        "holding_period_data_complete": False,
        "snapshot_at": NOW.isoformat(),
        "schema_version": "1.0",
    }


def test_local_api_runs_position_crud_and_snapshot_flow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with client(tmp_path) as api:
            assert (
                await api.post("/api/v1/portfolio/profiles", json=profile_payload())
            ).status_code == 201
            assert (
                await api.post("/api/v1/portfolio/assets", json=asset_payload())
            ).status_code == 201
            created = await api.post("/api/v1/portfolio/positions", json=position_payload())
            assert created.status_code == 201
            assert "653.59" not in caplog.text

            updated_payload = position_payload()
            updated_payload["current_value"] = money("600").model_dump(mode="json")
            updated = await api.put(
                "/api/v1/portfolio/positions/position-007300", json=updated_payload
            )
            assert updated.status_code == 200
            assert updated.json()["current_value"]["amount"] == "600"

            snapshot = await api.post(
                "/api/v1/portfolio/positions/position-007300/analysis-snapshots"
            )
            assert snapshot.status_code == 201
            snapshot_id = snapshot.json()["snapshot_id"]
            summary = await api.get(
                f"/api/v1/portfolio/analysis-snapshots/{snapshot_id}/ai-risk-summary"
            )
            assert summary.status_code == 200
            for forbidden in ("600", "640", "157.89", "profile-demo"):
                assert forbidden not in summary.text

            assert (
                await api.delete("/api/v1/portfolio/positions/position-007300")
            ).status_code == 204
            assert (await api.get("/api/v1/portfolio/positions/position-007300")).status_code == 404

    asyncio.run(scenario())


def test_portfolio_api_rejects_remote_and_cross_origin_access(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client(tmp_path, client_host="192.0.2.10") as remote:
            assert (await remote.get("/api/v1/portfolio/positions")).status_code == 403

        async with client(tmp_path) as local:
            response = await local.get(
                "/api/v1/portfolio/positions",
                headers={"Origin": "https://attacker.example"},
            )
            assert response.status_code == 403

        async with client(
            tmp_path,
            service_host="0.0.0.0",  # noqa: S104
        ) as publicly_bound:
            assert (await publicly_bound.get("/api/v1/portfolio/positions")).status_code == 403

    asyncio.run(scenario())


def test_api_rejects_invalid_amount_unknown_fields_and_id_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client(tmp_path) as api:
            profile = profile_payload()
            profile["account_number"] = "must-never-be-accepted"
            assert (await api.post("/api/v1/portfolio/profiles", json=profile)).status_code == 422

            assert (
                await api.post("/api/v1/portfolio/profiles", json=profile_payload())
            ).status_code == 201
            assert (
                await api.post("/api/v1/portfolio/assets", json=asset_payload())
            ).status_code == 201
            invalid = position_payload()
            invalid["units"] = "-1"
            assert (await api.post("/api/v1/portfolio/positions", json=invalid)).status_code == 422

            response = await api.put(
                "/api/v1/portfolio/positions/another-id",
                json=position_payload(),
            )
            assert response.status_code == 422

    asyncio.run(scenario())


def test_asset_classification_and_duplicate_conflicts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client(tmp_path) as api:
            classified = await api.post(
                "/api/v1/portfolio/classify-asset",
                json={
                    "exchange_traded": False,
                    "feeder_fund": True,
                    "index_tracking": True,
                },
            )
            assert classified.status_code == 200
            assert classified.json() == {"asset_type": "ETF_LINK"}

            contradictory = await api.post(
                "/api/v1/portfolio/classify-asset",
                json={
                    "exchange_traded": True,
                    "feeder_fund": True,
                    "index_tracking": True,
                },
            )
            assert contradictory.status_code == 422

            assert (
                await api.post("/api/v1/portfolio/profiles", json=profile_payload())
            ).status_code == 201
            assert (
                await api.post("/api/v1/portfolio/profiles", json=profile_payload())
            ).status_code == 409
            assert (
                await api.post("/api/v1/portfolio/assets", json=asset_payload())
            ).status_code == 201
            assert (
                await api.post("/api/v1/portfolio/assets", json=asset_payload())
            ).status_code == 409

    asyncio.run(scenario())
