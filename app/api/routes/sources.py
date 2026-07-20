"""Loopback-only source registry API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.collectors.registry import AdapterRegistry
from app.domain.collection import SourceAdapterState, SourceHealthSnapshot, SourceOperationalStatus
from app.domain.taxonomy import Source
from app.security.local_access import require_local_access
from app.services.scheduler import is_source_stale
from app.services.sources import SourceConflict, SourceNotFound, SourceService
from app.storage.database import Database

router = APIRouter(
    prefix="/sources",
    tags=["sources"],
    dependencies=[Depends(require_local_access)],
)


def source_service(request: Request) -> Iterator[SourceService]:
    database: Database = request.app.state.database
    workspace_id: str = request.app.state.settings.portfolio_workspace_id
    registry: AdapterRegistry = request.app.state.adapter_registry
    with database.session() as session:
        yield SourceService(session, workspace_id, registry)


SourceServiceDependency = Annotated[SourceService, Depends(source_service)]


def _not_found(error: SourceNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _conflict(error: SourceConflict) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@router.post("", response_model=Source, status_code=status.HTTP_201_CREATED)
def create_source(source: Source, service: SourceServiceDependency) -> Source:
    try:
        return service.create(source)
    except SourceConflict as error:
        raise _conflict(error) from error


@router.get("", response_model=list[Source])
def list_sources(service: SourceServiceDependency) -> tuple[Source, ...]:
    return service.list()


@router.get("/schedulable", response_model=list[Source])
def list_schedulable_sources(service: SourceServiceDependency) -> tuple[Source, ...]:
    return service.list_schedulable()


@router.get("/{source_id}", response_model=Source)
def get_source(source_id: str, service: SourceServiceDependency) -> Source:
    try:
        return service.get(source_id)
    except SourceNotFound as error:
        raise _not_found(error) from error


@router.put("/{source_id}", response_model=Source)
def update_source(
    source_id: str,
    source: Source,
    service: SourceServiceDependency,
) -> Source:
    try:
        return service.update(source_id, source)
    except SourceNotFound as error:
        raise _not_found(error) from error
    except SourceConflict as error:
        raise _conflict(error) from error


@router.delete("/{source_id}", response_model=Source)
def disable_source(source_id: str, service: SourceServiceDependency) -> Source:
    try:
        return service.disable(source_id)
    except SourceNotFound as error:
        raise _not_found(error) from error


@router.get("/{source_id}/health", response_model=SourceHealthSnapshot)
def get_source_health(
    source_id: str,
    service: SourceServiceDependency,
) -> SourceHealthSnapshot:
    try:
        return service.health(source_id)
    except SourceNotFound as error:
        raise _not_found(error) from error


@router.get("/{source_id}/adapter-state", response_model=SourceAdapterState | None)
def get_source_adapter_state(
    source_id: str,
    service: SourceServiceDependency,
) -> SourceAdapterState | None:
    try:
        return service.adapter_state(source_id)
    except SourceNotFound as error:
        raise _not_found(error) from error


@router.get("/{source_id}/status", response_model=SourceOperationalStatus)
def get_source_operational_status(
    source_id: str,
    service: SourceServiceDependency,
) -> SourceOperationalStatus:
    try:
        source = service.get(source_id)
        health = service.health(source_id)
    except SourceNotFound as error:
        raise _not_found(error) from error
    return SourceOperationalStatus(
        source_id=source_id,
        enabled=source.enabled,
        health=health,
        is_stale=is_source_stale(health, datetime.now(UTC)),
    )
