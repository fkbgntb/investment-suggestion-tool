from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes import dashboard
from app.config import Settings
from app.main import create_app
from app.services.manual_pipeline import ManualPipelineOutcome
from app.services.portfolio import PortfolioService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from tests.domain_factories import investment_profile
from tests.test_portfolio_service import asset, position


def application(tmp_path: Path) -> FastAPI:
    data_dir = tmp_path / "data"
    database_url = f"sqlite:///{(data_dir / 'web.sqlite3').as_posix()}"
    upgrade_database(database_url)
    database = Database(database_url)
    try:
        with database.session() as session:
            service = PortfolioService(session, "personal-demo")
            service.create_profile(investment_profile())
            service.create_asset(asset())
            service.create_position(position())
    finally:
        database.dispose()
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            data_dir=data_dir,
            database_url=database_url,
            portfolio_reference_value="3000",
        )
    )


async def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1")


def test_all_personal_pages_load_without_external_assets(tmp_path: Path) -> None:
    async def run() -> None:
        app = application(tmp_path)
        async with await client_for(app) as client:
            for path in (
                "/",
                "/portfolio",
                "/evidence",
                "/reports",
                "/sources",
                "/jobs",
                "/settings",
            ):
                response = await client.get(path)
                assert response.status_code == 200
                assert "/static/app.css" in response.text
                assert "investment_csrf" in response.cookies
            css = await client.get("/static/app.css")
            javascript = await client.get("/static/app.js")
            assert css.status_code == javascript.status_code == 200
            assert "https://" not in css.text
            assert "innerHTML" not in javascript.text
        app.state.database.dispose()

    asyncio.run(run())


def test_csrf_origin_and_public_bind_boundaries(tmp_path: Path) -> None:
    async def run() -> None:
        app = application(tmp_path)
        async with await client_for(app) as client:
            missing = await client.post(
                "/api/v1/analysis/run",
                json={"position_id": "position-007300"},
            )
            assert missing.status_code == 403
            cross_origin = await client.get(
                "/api/v1/positions", headers={"origin": "https://evil.example"}
            )
            assert cross_origin.status_code == 403
            assert "access-control-allow-origin" not in cross_origin.headers

        public_settings = app.state.settings.model_copy(
            update={"host": "0.0.0.0", "allow_public_bind": True}  # noqa: S104
        )
        public_app = create_app(public_settings)
        async with await client_for(public_app) as client:
            assert (await client.get("/api/v1/positions")).status_code == 403
            assert (await client.get("/")).status_code == 403
        app.state.database.dispose()
        public_app.state.database.dispose()

    asyncio.run(run())


def test_local_analysis_api_completes_degraded_report_without_key(tmp_path: Path) -> None:
    async def run() -> None:
        app = application(tmp_path)
        token = app.state.csrf_token
        async with await client_for(app) as client:
            root = await client.get("/")
            assert root.status_code == 200
            response = await client.post(
                "/api/v1/analysis/run",
                headers={"x-investment-csrf": token},
                json={"position_id": "position-007300"},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["decision"]["label"] == "INSUFFICIENT_DATA"
            assert payload["analysis"]["degraded"] is True
            assert payload["report"]["advisory_only"] is True
            report_id = payload["report"]["report_id"]
            html = await client.get(f"/api/v1/reports/{report_id}/html")
            assert html.status_code == 200
            assert "系统不执行交易" in html.text
            assert "script-src 'none'" in html.headers["content-security-policy"]
            assert "frame-ancestors 'self'" in html.headers["content-security-policy"]
            assert html.headers["x-frame-options"] == "SAMEORIGIN"
            health = await client.get("/api/v1/health")
            assert health.headers["x-frame-options"] == "DENY"
            latest = await client.get("/api/v1/reports/latest")
            assert latest.json()["report_id"] == report_id
        app.state.database.dispose()

    asyncio.run(run())


def test_position_update_and_manual_crawl_require_valid_local_session(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_pipeline(*args, **kwargs) -> ManualPipelineOutcome:
        return ManualPipelineOutcome(
            source_count=1,
            failed_source_count=0,
            new_document_count=2,
            normalized_count=2,
            duplicate_count=0,
            quarantined_count=0,
            relevant_count=1,
            review_count=0,
            irrelevant_count=1,
            extraction_count=1,
            extraction_review_count=0,
            scored_count=1,
        )

    monkeypatch.setattr(dashboard, "run_manual_pipeline", fake_pipeline)

    async def run() -> None:
        app = application(tmp_path)
        token = app.state.csrf_token
        async with await client_for(app) as client:
            await client.get("/")
            update = await client.post(
                "/api/v1/positions/position-007300/local-update",
                headers={"x-investment-csrf": token},
                json={
                    "units": "157.89",
                    "current_value": {"amount": "620", "currency": "CNY"},
                    "average_cost_per_unit": "4.0535",
                    "latest_purchase_on": "2026-07-01",
                    "recurring_contribution": {"amount": "50", "currency": "CNY"},
                },
            )
            assert update.status_code == 200
            assert update.json()["current_value"]["amount"] == "620"
            crawl = await client.post("/api/v1/jobs/crawl", headers={"x-investment-csrf": token})
            assert crawl.status_code == 200
            assert crawl.json()["new_document_count"] == 2
            settings = await client.get("/api/v1/settings/public")
            assert settings.json()["deepseek_configured"] is False
            assert settings.json()["alpha_vantage_configured"] is False
            assert "api_key" not in settings.text.casefold()
        app.state.database.dispose()

    asyncio.run(run())
