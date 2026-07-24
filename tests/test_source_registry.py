from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.collectors.registry import AdapterNotRegistered, AdapterRegistry
from app.config import Settings
from app.domain.enums import SourceHealthStatus, SourceKind, TrustTier
from app.domain.taxonomy import Source
from app.main import create_app
from app.services.sources import SourceConflict, SourceService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import AuditEventRow


def source(*, enabled: bool = True, adapter_name: str = "mock-rss") -> Source:
    return Source(
        source_id="mock-semiconductor-news",
        name="Mock semiconductor RSS",
        kind=SourceKind.NEWS,
        trust_tier=TrustTier.SECONDARY,
        base_url="https://feeds.example.com/semiconductor.xml",
        regions=("GLOBAL",),
        languages=("en",),
        enabled=enabled,
        adapter_name=adapter_name,
        crawl_interval_hours=3,
        allow_fulltext=False,
        allowed_domains=("feeds.example.com",),
        terms_url="https://feeds.example.com/terms",
        config_version="test-1",
    )


def database(tmp_path: Path) -> Database:
    database_url = f"sqlite:///{(tmp_path / 'sources.sqlite3').as_posix()}"
    upgrade_database(database_url)
    return Database(database_url)


def test_registry_is_an_explicit_whitelist() -> None:
    registry = AdapterRegistry(("mock-rss",))
    registry.require("mock-rss")
    with pytest.raises(AdapterNotRegistered):
        registry.require("path.to.arbitrary.module")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("mock-rss")


def test_source_crud_disable_health_cursor_and_audit(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            service = SourceService(session, "personal", AdapterRegistry(("mock-rss",)))
            created = service.create(source())
            assert service.get(created.source_id) == created
            assert service.list_schedulable() == (created,)
            now = datetime(2026, 7, 20, tzinfo=UTC)
            assert service.list_due(now=now) == (created,)
            service.record_health(
                service.health(created.source_id).model_copy(update={"last_success_at": now})
            )
            assert service.list_due(now=now + timedelta(hours=2)) == ()
            assert service.list_due(now=now + timedelta(hours=3)) == (created,)
            assert service.health(created.source_id).status is SourceHealthStatus.UNKNOWN

            state = service.advance_cursor(
                created.source_id,
                adapter_version="1.0",
                cursor="page-2",
                expected_version=0,
                occurred_at=datetime(2026, 7, 20, tzinfo=UTC),
            )
            assert state.state_version == 1
            assert service.adapter_state(created.source_id) == state
            with pytest.raises(SourceConflict, match="version"):
                service.advance_cursor(
                    created.source_id,
                    adapter_version="1.0",
                    cursor="stale",
                    expected_version=0,
                )

            disabled = service.disable(created.source_id)
            assert disabled.enabled is False
            assert service.list_schedulable() == ()
            assert service.health(created.source_id).status is SourceHealthStatus.DISABLED
            assert service.adapter_state(created.source_id) == state
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEventRow)
                    .where(AuditEventRow.target_type == "source")
                )
                == 2
            )
    finally:
        db.dispose()


def test_service_rejects_unregistered_adapter_and_unsafe_source_url(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            service = SourceService(session, "personal", AdapterRegistry(("mock-rss",)))
            with pytest.raises(SourceConflict, match="not registered"):
                service.create(source(adapter_name="unknown"))
        with pytest.raises(ValueError, match="explicitly allowed"):
            Source.model_validate(
                {**source().model_dump(mode="json"), "allowed_domains": ("other.example",)}
            )
    finally:
        db.dispose()


@asynccontextmanager
async def api_client(
    tmp_path: Path,
    *,
    client_host: str = "127.0.0.1",
) -> AsyncIterator[AsyncClient]:
    data_dir = tmp_path / client_host.replace(".", "-")
    database_url = f"sqlite:///{(data_dir / 'api.sqlite3').as_posix()}"
    upgrade_database(database_url)
    application = create_app(Settings(_env_file=None, data_dir=data_dir, database_url=database_url))
    transport = ASGITransport(app=application, client=(client_host, 50_000))
    try:
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            yield client
    finally:
        application.state.database.dispose()


def test_local_source_api_and_remote_access_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        payload = source().model_dump(mode="json")
        async with api_client(tmp_path) as client:
            created = await client.post("/api/v1/sources", json=payload)
            assert created.status_code == 201
            assert len((await client.get("/api/v1/sources/schedulable")).json()) == 1
            assert (
                await client.post(
                    "/api/v1/sources",
                    json={**payload, "source_id": "bad", "adapter_name": "unregistered"},
                )
            ).status_code == 409
            disabled = await client.delete(f"/api/v1/sources/{payload['source_id']}")
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False
            health = await client.get(f"/api/v1/sources/{payload['source_id']}/health")
            assert health.json()["status"] == "DISABLED"
            assert (await client.get("/api/v1/sources/schedulable")).json() == []

        async with api_client(tmp_path, client_host="192.0.2.10") as remote:
            assert (await remote.get("/api/v1/sources")).status_code == 403

    asyncio.run(scenario())
