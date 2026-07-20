"""Local-only relevance review endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import RelevanceLabel
from app.domain.relevance import HumanRelevanceLabel
from app.security.local_access import require_local_access
from app.services.relevance import RelevanceService
from app.storage.database import Database

router = APIRouter(
    prefix="/relevance",
    tags=["relevance"],
    dependencies=[Depends(require_local_access)],
)


class HumanLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: RelevanceLabel
    note: str | None = Field(default=None, max_length=1000)


def relevance_service(request: Request) -> Iterator[RelevanceService]:
    database: Database = request.app.state.database
    workspace_id: str = request.app.state.settings.portfolio_workspace_id
    with database.session() as session:
        yield RelevanceService(session, workspace_id)


RelevanceServiceDependency = Annotated[RelevanceService, Depends(relevance_service)]


@router.post("/documents/{document_id}/labels", response_model=HumanRelevanceLabel)
def label_document(
    document_id: str,
    payload: HumanLabelRequest,
    service: RelevanceServiceDependency,
) -> HumanRelevanceLabel:
    try:
        return service.add_human_label(
            document_id,
            payload.label,
            note=payload.note,
            now=datetime.now(UTC),
        )
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
