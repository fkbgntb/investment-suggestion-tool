from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import DocumentState, TransitionOutcome
from app.domain.state_machine import (
    InvalidDocumentTransition,
    evaluate_document_transition,
    require_document_transition,
)

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
FORWARD_PATH = (
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


def test_complete_document_path_is_explicit_and_auditable() -> None:
    version = 0
    records = []
    pairs = zip(FORWARD_PATH[:-1], FORWARD_PATH[1:], strict=True)
    for index, (current, requested) in enumerate(pairs):
        record = require_document_transition(
            document_id="document-1",
            current=current,
            requested=requested,
            state_version=version,
            occurred_at=NOW + timedelta(seconds=index),
        )
        records.append(record)
        version = record.next_version

    assert all(record.outcome is TransitionOutcome.APPLIED for record in records)
    assert version == len(FORWARD_PATH) - 1
    assert records[-1].requested_state is DocumentState.PUBLISHED


def test_repeated_step_is_a_stable_noop() -> None:
    first = evaluate_document_transition(
        document_id="document-1",
        current=DocumentState.FETCHED,
        requested=DocumentState.FETCHED,
        state_version=1,
        occurred_at=NOW,
    )
    second = evaluate_document_transition(
        document_id="document-1",
        current=DocumentState.FETCHED,
        requested=DocumentState.FETCHED,
        state_version=1,
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert first.outcome is TransitionOutcome.NOOP
    assert first.next_version == first.previous_version
    assert first.transition_id == second.transition_id


def test_raw_document_cannot_jump_directly_to_published() -> None:
    record = evaluate_document_transition(
        document_id="document-1",
        current=DocumentState.DISCOVERED,
        requested=DocumentState.PUBLISHED,
        state_version=0,
        occurred_at=NOW,
    )
    assert record.outcome is TransitionOutcome.REJECTED
    assert record.next_version == 0
    assert "not allowed" in record.reason

    with pytest.raises(InvalidDocumentTransition) as error:
        require_document_transition(
            document_id="document-1",
            current=DocumentState.DISCOVERED,
            requested=DocumentState.PUBLISHED,
            state_version=0,
            occurred_at=NOW,
        )
    assert error.value.record == record


def test_failure_and_retry_paths_are_controlled() -> None:
    failure = require_document_transition(
        document_id="document-1",
        current=DocumentState.NORMALIZED,
        requested=DocumentState.RETRYABLE_FAILED,
        state_version=2,
        occurred_at=NOW,
    )
    retry = require_document_transition(
        document_id="document-1",
        current=DocumentState.RETRYABLE_FAILED,
        requested=DocumentState.DISCOVERED,
        state_version=failure.next_version,
        occurred_at=NOW,
    )
    terminal = evaluate_document_transition(
        document_id="document-1",
        current=DocumentState.QUARANTINED,
        requested=DocumentState.DISCOVERED,
        state_version=4,
        occurred_at=NOW,
    )

    assert retry.outcome is TransitionOutcome.APPLIED
    assert terminal.outcome is TransitionOutcome.REJECTED


def test_negative_state_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        evaluate_document_transition(
            document_id="document-1",
            current=DocumentState.DISCOVERED,
            requested=DocumentState.FETCHED,
            state_version=-1,
            occurred_at=NOW,
        )
