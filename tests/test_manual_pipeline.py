from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from app.config import Settings
from app.services import manual_pipeline


class FakeDatabase:
    @contextmanager
    def session(self):
        yield object()


class FakeHttpClient:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _install_processing_fakes(monkeypatch, *, relevance_raises: bool = False) -> None:
    class FakeNormalization:
        def __init__(self, *args, **kwargs):
            pass

        def process_pending(self, *, now):
            return (3, 1, 1)

    class FakeRelevance:
        def __init__(self, *args, **kwargs):
            pass

        def classify_pending(self, *, now):
            if relevance_raises:
                raise RuntimeError("taxonomy unavailable")
            return (2, 1, 0)

    class FakeExtraction:
        def __init__(self, *args, **kwargs):
            pass

        async def extract_pending(self, *, now):
            return (2, 1)

    class FakeScoring:
        def __init__(self, *args, **kwargs):
            pass

        def score_pending(self, *, now):
            return (2, 0)

    monkeypatch.setattr(manual_pipeline, "NormalizationService", FakeNormalization)
    monkeypatch.setattr(manual_pipeline, "RelevanceService", FakeRelevance)
    monkeypatch.setattr(manual_pipeline, "EvidenceExtractionService", FakeExtraction)
    monkeypatch.setattr(manual_pipeline, "EvidenceScoringService", FakeScoring)
    monkeypatch.setattr(manual_pipeline, "build_evidence_provider", lambda settings: object())
    monkeypatch.setattr(
        manual_pipeline, "build_safe_http_client", lambda settings: FakeHttpClient()
    )
    monkeypatch.setattr(manual_pipeline, "build_default_adapter_registry", object)


def test_manual_pipeline_isolates_sources_and_processes_pending(monkeypatch, tmp_path) -> None:
    sources = (
        SimpleNamespace(source_id="gdelt", adapter_name="gdelt-doc"),
        SimpleNamespace(source_id="sec", adapter_name="sec-submissions"),
        SimpleNamespace(source_id="unsupported", adapter_name="other"),
    )

    class FakeSourceService:
        def __init__(self, *args, **kwargs):
            pass

        def list_schedulable(self):
            return sources

    class FakeGdelt:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return SimpleNamespace(created_count=2, status="SUCCEEDED")

    monkeypatch.setattr(manual_pipeline, "SourceService", FakeSourceService)
    monkeypatch.setattr(manual_pipeline, "GDELTCollectionService", FakeGdelt)
    _install_processing_fakes(monkeypatch)
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'pipeline.sqlite3').as_posix()}",
    )

    outcome = asyncio.run(
        manual_pipeline.run_manual_pipeline(
            FakeDatabase(), settings, now=datetime(2026, 7, 20, tzinfo=UTC)
        )
    )

    assert outcome.as_dict() == {
        "source_count": 3,
        "failed_source_count": 2,
        "new_document_count": 2,
        "normalized_count": 3,
        "duplicate_count": 1,
        "quarantined_count": 1,
        "relevant_count": 2,
        "review_count": 1,
        "irrelevant_count": 0,
        "extraction_count": 2,
        "extraction_review_count": 1,
        "scored_count": 2,
    }


def test_manual_pipeline_runs_sec_and_degrades_relevance_failure(monkeypatch, tmp_path) -> None:
    sources = (SimpleNamespace(source_id="sec", adapter_name="sec-submissions"),)

    class FakeSourceService:
        def __init__(self, *args, **kwargs):
            pass

        def list_schedulable(self):
            return sources

    class FakeSec:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return SimpleNamespace(created_count=1, status="FAILED")

    monkeypatch.setattr(manual_pipeline, "SourceService", FakeSourceService)
    monkeypatch.setattr(manual_pipeline, "SECCollectionService", FakeSec)
    monkeypatch.setattr(manual_pipeline, "_sec_companies", lambda: ())
    _install_processing_fakes(monkeypatch, relevance_raises=True)
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'pipeline.sqlite3').as_posix()}",
        sec_contact_email="owner@example.com",
        deepseek_api_key="test-key",
    )

    outcome = asyncio.run(
        manual_pipeline.run_manual_pipeline(
            FakeDatabase(), settings, now=datetime(2026, 7, 20, tzinfo=UTC)
        )
    )

    assert outcome.failed_source_count == 1
    assert outcome.new_document_count == 1
    assert (outcome.relevant_count, outcome.review_count, outcome.irrelevant_count) == (0, 0, 0)


def test_manual_pipeline_runs_configured_alpha_vantage(monkeypatch, tmp_path) -> None:
    sources = (SimpleNamespace(source_id="alpha", adapter_name="alpha-vantage-news"),)

    class FakeSourceService:
        def __init__(self, *args, **kwargs):
            pass

        def list_schedulable(self):
            return sources

    class FakeAlphaVantage:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return SimpleNamespace(created_count=4, status="SUCCEEDED")

    monkeypatch.setattr(manual_pipeline, "SourceService", FakeSourceService)
    monkeypatch.setattr(manual_pipeline, "AlphaVantageCollectionService", FakeAlphaVantage)
    _install_processing_fakes(monkeypatch)
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'pipeline.sqlite3').as_posix()}",
        alpha_vantage_api_key="test-key",
    )

    outcome = asyncio.run(
        manual_pipeline.run_manual_pipeline(
            FakeDatabase(), settings, now=datetime(2026, 7, 20, tzinfo=UTC)
        )
    )

    assert outcome.failed_source_count == 0
    assert outcome.new_document_count == 4
