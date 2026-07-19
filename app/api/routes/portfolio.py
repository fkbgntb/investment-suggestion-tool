"""Local-only portfolio CRUD and privacy-minimized analysis snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.config import Settings
from app.domain.enums import AssetType
from app.domain.portfolio import (
    Asset,
    InvestmentProfile,
    PortfolioAIRiskSummary,
    Position,
    PositionAnalysisSnapshot,
)
from app.services.portfolio import (
    PortfolioConflict,
    PortfolioNotFound,
    PortfolioService,
    identify_asset_type,
    is_loopback_client,
)
from app.storage.database import Database

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


class AssetClassificationRequest(BaseModel):
    exchange_traded: bool
    feeder_fund: bool
    index_tracking: bool


class AssetClassificationResponse(BaseModel):
    asset_type: AssetType


def require_local_portfolio_access(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not is_loopback_client(settings.host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="portfolio API is disabled when the service has a public bind address",
        )
    client_host = request.client.host if request.client is not None else None
    if not is_loopback_client(client_host):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local access only")
    origin = request.headers.get("origin")
    if origin is not None and not is_loopback_client(urlsplit(origin).hostname):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-origin portfolio access is forbidden",
        )


def portfolio_service(request: Request) -> Iterator[PortfolioService]:
    database: Database = request.app.state.database
    settings: Settings = request.app.state.settings
    with database.session() as session:
        yield PortfolioService(session, settings.portfolio_workspace_id)


PortfolioServiceDependency = Annotated[PortfolioService, Depends(portfolio_service)]


def _not_found(error: PortfolioNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _conflict(error: PortfolioConflict) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@router.post(
    "/classify-asset",
    response_model=AssetClassificationResponse,
    dependencies=[Depends(require_local_portfolio_access)],
)
def classify_asset(request: AssetClassificationRequest) -> AssetClassificationResponse:
    try:
        asset_type = identify_asset_type(**request.model_dump())
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    return AssetClassificationResponse(asset_type=asset_type)


@router.post(
    "/profiles",
    response_model=InvestmentProfile,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_local_portfolio_access)],
)
def create_profile(
    profile: InvestmentProfile,
    service: PortfolioServiceDependency,
) -> InvestmentProfile:
    try:
        return service.create_profile(profile)
    except PortfolioConflict as error:
        raise _conflict(error) from error


@router.get(
    "/profiles",
    response_model=list[InvestmentProfile],
    dependencies=[Depends(require_local_portfolio_access)],
)
def list_profiles(
    service: PortfolioServiceDependency,
) -> tuple[InvestmentProfile, ...]:
    return service.list_profiles()


@router.put(
    "/profiles/{profile_id}",
    response_model=InvestmentProfile,
    dependencies=[Depends(require_local_portfolio_access)],
)
def update_profile(
    profile_id: str,
    profile: InvestmentProfile,
    service: PortfolioServiceDependency,
) -> InvestmentProfile:
    if profile.profile_id != profile_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="profile ID mismatch"
        )
    try:
        return service.update_profile(profile)
    except PortfolioNotFound as error:
        raise _not_found(error) from error


@router.post(
    "/assets",
    response_model=Asset,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_local_portfolio_access)],
)
def create_asset(
    asset: Asset,
    service: PortfolioServiceDependency,
) -> Asset:
    try:
        return service.create_asset(asset)
    except PortfolioConflict as error:
        raise _conflict(error) from error


@router.get(
    "/assets",
    response_model=list[Asset],
    dependencies=[Depends(require_local_portfolio_access)],
)
def list_assets(service: PortfolioServiceDependency) -> tuple[Asset, ...]:
    return service.list_assets()


@router.put(
    "/assets/{asset_id}",
    response_model=Asset,
    dependencies=[Depends(require_local_portfolio_access)],
)
def update_asset(
    asset_id: str,
    asset: Asset,
    service: PortfolioServiceDependency,
) -> Asset:
    if asset.asset_id != asset_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="asset ID mismatch"
        )
    try:
        return service.update_asset(asset)
    except PortfolioNotFound as error:
        raise _not_found(error) from error
    except PortfolioConflict as error:
        raise _conflict(error) from error


@router.post(
    "/positions",
    response_model=Position,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_local_portfolio_access)],
)
def create_position(
    position: Position,
    service: PortfolioServiceDependency,
) -> Position:
    try:
        return service.create_position(position)
    except PortfolioConflict as error:
        raise _conflict(error) from error


@router.get(
    "/positions",
    response_model=list[Position],
    dependencies=[Depends(require_local_portfolio_access)],
)
def list_positions(service: PortfolioServiceDependency) -> tuple[Position, ...]:
    return service.list_positions()


@router.get(
    "/positions/{position_id}",
    response_model=Position,
    dependencies=[Depends(require_local_portfolio_access)],
)
def get_position(
    position_id: str,
    service: PortfolioServiceDependency,
) -> Position:
    try:
        return service.get_position(position_id)
    except PortfolioNotFound as error:
        raise _not_found(error) from error


@router.put(
    "/positions/{position_id}",
    response_model=Position,
    dependencies=[Depends(require_local_portfolio_access)],
)
def update_position(
    position_id: str,
    position: Position,
    service: PortfolioServiceDependency,
) -> Position:
    if position.position_id != position_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="position ID mismatch"
        )
    try:
        return service.update_position(position)
    except PortfolioNotFound as error:
        raise _not_found(error) from error
    except PortfolioConflict as error:
        raise _conflict(error) from error


@router.delete(
    "/positions/{position_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_local_portfolio_access)],
)
def delete_position(
    position_id: str,
    service: PortfolioServiceDependency,
) -> Response:
    try:
        service.delete_position(position_id)
    except PortfolioNotFound as error:
        raise _not_found(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/positions/{position_id}/analysis-snapshots",
    response_model=PositionAnalysisSnapshot,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_local_portfolio_access)],
)
def create_analysis_snapshot(
    position_id: str,
    service: PortfolioServiceDependency,
) -> PositionAnalysisSnapshot:
    try:
        return service.create_analysis_snapshot(position_id)
    except PortfolioNotFound as error:
        raise _not_found(error) from error


@router.get(
    "/analysis-snapshots/{snapshot_id}/ai-risk-summary",
    response_model=PortfolioAIRiskSummary,
    dependencies=[Depends(require_local_portfolio_access)],
)
def get_ai_risk_summary(
    snapshot_id: str,
    service: PortfolioServiceDependency,
) -> PortfolioAIRiskSummary:
    try:
        return service.build_ai_risk_summary(snapshot_id)
    except PortfolioNotFound as error:
        raise _not_found(error) from error
