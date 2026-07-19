"""Pure, auditable document pipeline state transition rules."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, Field

from app.domain.base import DomainModel, Identifier
from app.domain.enums import DocumentState, TransitionOutcome

_FORWARD_PATH = (
    DocumentState.DISCOVERED,
    DocumentState.FETCHED,
    DocumentState.NORMALIZED,
    DocumentState.DEDUPLICATED,
    DocumentState.CLASSIFIED,
    DocumentState.EXTRACTED,
    DocumentState.SCORED,
    DocumentState.ANALYZED,
    DocumentState.PUBLISHED,
)
_FAILURE_TARGETS = frozenset(
    {
        DocumentState.RETRYABLE_FAILED,
        DocumentState.PERMANENT_FAILED,
        DocumentState.QUARANTINED,
    }
)

_ALLOWED_TRANSITIONS: dict[DocumentState, frozenset[DocumentState]] = {
    state: frozenset({next_state, *_FAILURE_TARGETS})
    for state, next_state in zip(_FORWARD_PATH[:-1], _FORWARD_PATH[1:], strict=True)
}
_ALLOWED_TRANSITIONS[DocumentState.RETRYABLE_FAILED] = frozenset(
    {DocumentState.DISCOVERED, DocumentState.PERMANENT_FAILED, DocumentState.QUARANTINED}
)
_ALLOWED_TRANSITIONS[DocumentState.PUBLISHED] = frozenset()
_ALLOWED_TRANSITIONS[DocumentState.PERMANENT_FAILED] = frozenset()
_ALLOWED_TRANSITIONS[DocumentState.QUARANTINED] = frozenset()


class StateTransitionRecord(DomainModel):
    transition_id: Identifier
    document_id: Identifier
    from_state: DocumentState
    requested_state: DocumentState
    outcome: TransitionOutcome
    previous_version: int = Field(ge=0)
    next_version: int = Field(ge=0)
    occurred_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=500)


class InvalidDocumentTransition(ValueError):
    """Raised only by the strict helper; contains an auditable rejection record."""

    def __init__(self, record: StateTransitionRecord) -> None:
        self.record = record
        super().__init__(record.reason)


def evaluate_document_transition(
    *,
    document_id: Identifier,
    current: DocumentState,
    requested: DocumentState,
    state_version: int,
    occurred_at: AwareDatetime,
) -> StateTransitionRecord:
    """Evaluate a transition without mutation; repeated transitions become stable no-ops."""
    if state_version < 0:
        raise ValueError("state version cannot be negative")

    if current is requested:
        outcome = TransitionOutcome.NOOP
        next_version = state_version
        reason = "requested state is already current; no write is required"
    elif requested in _ALLOWED_TRANSITIONS[current]:
        outcome = TransitionOutcome.APPLIED
        next_version = state_version + 1
        reason = "transition is allowed"
    else:
        outcome = TransitionOutcome.REJECTED
        next_version = state_version
        reason = f"transition from {current.value} to {requested.value} is not allowed"

    stable_key = f"{document_id}:{current.value}:{requested.value}:{state_version}"
    return StateTransitionRecord(
        transition_id=str(uuid5(NAMESPACE_URL, stable_key)),
        document_id=document_id,
        from_state=current,
        requested_state=requested,
        outcome=outcome,
        previous_version=state_version,
        next_version=next_version,
        occurred_at=occurred_at,
        reason=reason,
    )


def require_document_transition(
    *,
    document_id: Identifier,
    current: DocumentState,
    requested: DocumentState,
    state_version: int,
    occurred_at: AwareDatetime,
) -> StateTransitionRecord:
    """Return an accepted/no-op transition or reject with its audit record attached."""
    record = evaluate_document_transition(
        document_id=document_id,
        current=current,
        requested=requested,
        state_version=state_version,
        occurred_at=occurred_at,
    )
    if record.outcome is TransitionOutcome.REJECTED:
        raise InvalidDocumentTransition(record)
    return record
