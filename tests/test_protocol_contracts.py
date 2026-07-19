import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.contracts import (
    AIProvider,
    DecisionPolicy,
    DispatchReceipt,
    MarketDataProvider,
    NotificationProvider,
    ReportRenderer,
    SourceAdapter,
    SourceDiscoveryRequest,
    SourceFetchResult,
    StorageProvider,
    StorageRecord,
    StorageWriteRequest,
    StorageWriteResult,
    TaskDispatcher,
)
from app.domain.documents import ExternalDocumentContent
from tests.domain_factories import idempotency


class InMemoryStorage:
    provider_name = "memory"

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, object]] = {}

    async def save_if_absent(self, request: StorageWriteRequest) -> StorageWriteResult:
        key = (request.record_type, request.record_id)
        created = key not in self.records
        if created:
            self.records[key] = request.payload
        return StorageWriteResult(
            record_type=request.record_type,
            record_id=request.record_id,
            created=created,
            duplicate=not created,
        )

    async def get(self, record_type: str, record_id: str) -> StorageRecord | None:
        payload = self.records.get((record_type, record_id))
        if payload is None:
            return None
        return StorageRecord(record_type=record_type, record_id=record_id, payload=payload)


def test_storage_contract_makes_duplicate_writes_idempotent() -> None:
    storage = InMemoryStorage()
    request = StorageWriteRequest(
        record_type="evidence",
        record_id="evidence-1",
        payload={"claim": "safe structured value"},
        idempotency=idempotency(),
    )

    first = asyncio.run(storage.save_if_absent(request))
    second = asyncio.run(storage.save_if_absent(request))

    assert first.created is True
    assert second.duplicate is True
    assert len(storage.records) == 1
    stored = asyncio.run(storage.get("evidence", "evidence-1"))
    assert stored is not None
    assert stored.payload == request.payload


def test_storage_payload_rejects_non_json_executable_objects() -> None:
    with pytest.raises(ValidationError):
        StorageWriteRequest(
            record_type="unsafe",
            record_id="record-1",
            payload={"callable": lambda: None},
            idempotency=idempotency(),
        )


def test_public_interfaces_have_no_transaction_execution_capability() -> None:
    interfaces = (
        SourceAdapter,
        MarketDataProvider,
        AIProvider,
        DecisionPolicy,
        ReportRenderer,
        NotificationProvider,
        TaskDispatcher,
        StorageProvider,
    )
    forbidden = ("trade", "order", "redeem", "purchase", "transaction")
    method_names = {name.lower() for interface in interfaces for name in vars(interface)}
    assert not any(term in name for name in method_names for term in forbidden)


def test_runtime_protocol_recognizes_storage_shape() -> None:
    assert isinstance(InMemoryStorage(), StorageProvider)


def test_dispatch_receipt_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        DispatchReceipt(
            task_id="task-1",
            accepted=True,
            duplicate=False,
            executed_trade=True,
        )


def test_source_adapter_result_cannot_set_pipeline_control_fields() -> None:
    result = SourceFetchResult(
        source_id="source-1",
        content=ExternalDocumentContent(
            source_url="https://example.com/news/1",
            title="title",
            body="untrusted body",
            language="en",
        ),
        content_sha256="a" * 64,
        fetched_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )
    assert "state" not in SourceFetchResult.model_fields
    assert "control" not in SourceFetchResult.model_fields

    unsafe = result.model_dump(mode="json")
    unsafe["state"] = "PUBLISHED"
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceFetchResult.model_validate(unsafe)


def test_source_discovery_window_is_ordered() -> None:
    with pytest.raises(ValidationError, match="start cannot follow end"):
        SourceDiscoveryRequest(
            source_id="source-1",
            topic_ids=("semiconductor",),
            since=datetime(2026, 7, 20, tzinfo=UTC),
            until=datetime(2026, 7, 19, tzinfo=UTC),
        )
