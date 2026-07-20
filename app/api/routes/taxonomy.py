"""Local-only API for immutable taxonomy publication and activation."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.domain.taxonomy import TaxonomyConfiguration
from app.security.local_access import require_local_access
from app.services.taxonomy import TaxonomyConflict, TaxonomyNotFound, TaxonomyService
from app.storage.database import Database

router = APIRouter(
    prefix="/taxonomy",
    tags=["taxonomy"],
    dependencies=[Depends(require_local_access)],
)


def taxonomy_service(request: Request) -> Iterator[TaxonomyService]:
    database: Database = request.app.state.database
    workspace_id: str = request.app.state.settings.portfolio_workspace_id
    with database.session() as session:
        yield TaxonomyService(session, workspace_id)


TaxonomyServiceDependency = Annotated[TaxonomyService, Depends(taxonomy_service)]


def _not_found(error: TaxonomyNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _conflict(error: TaxonomyConflict) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@router.post(
    "/configurations",
    response_model=TaxonomyConfiguration,
    status_code=status.HTTP_201_CREATED,
)
def publish_configuration(
    configuration: TaxonomyConfiguration,
    service: TaxonomyServiceDependency,
) -> TaxonomyConfiguration:
    try:
        return service.publish(configuration)
    except TaxonomyConflict as error:
        raise _conflict(error) from error


@router.get("/configurations", response_model=list[TaxonomyConfiguration])
def list_configurations(
    service: TaxonomyServiceDependency,
) -> tuple[TaxonomyConfiguration, ...]:
    return service.list_configurations()


@router.get("/configurations/active", response_model=TaxonomyConfiguration)
def get_active_configuration(
    service: TaxonomyServiceDependency,
) -> TaxonomyConfiguration:
    try:
        return service.get_active()
    except TaxonomyNotFound as error:
        raise _not_found(error) from error


@router.get("/configurations/{config_version}", response_model=TaxonomyConfiguration)
def get_configuration(
    config_version: str,
    service: TaxonomyServiceDependency,
) -> TaxonomyConfiguration:
    try:
        return service.get(config_version)
    except TaxonomyNotFound as error:
        raise _not_found(error) from error


@router.post(
    "/configurations/{config_version}/activate",
    response_model=TaxonomyConfiguration,
)
def activate_configuration(
    config_version: str,
    service: TaxonomyServiceDependency,
) -> TaxonomyConfiguration:
    try:
        return service.activate(config_version)
    except TaxonomyNotFound as error:
        raise _not_found(error) from error
