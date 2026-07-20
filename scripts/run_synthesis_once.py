"""Synthesize pending deterministic decision runs once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.config import Settings
from app.services.analysis_synthesis import AnalysisSynthesisService, build_synthesis_provider
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


async def run() -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    provider = build_synthesis_provider(settings)
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            completed, degraded, budget_fallbacks = await AnalysisSynthesisService(
                session,
                settings.portfolio_workspace_id,
                provider,
                model_version=(
                    settings.deepseek_model
                    if settings.deepseek_api_key is not None
                    else "rules-1.0.0"
                ),
                max_calls_per_day=settings.deepseek_synthesis_max_calls_per_day,
                daily_token_budget=settings.deepseek_synthesis_daily_token_budget,
            ).synthesize_pending(now=datetime.now(UTC))
    finally:
        database.dispose()
    print(f"provider: {provider.provider_name}")
    print(f"completed: {completed}")
    print(f"degraded: {degraded}")
    print(f"budget fallbacks: {budget_fallbacks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
