"""Run the local advisory pipeline for one saved position."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.config import Settings
from app.domain.base import Money
from app.services.analysis_workflow import AnalysisWorkflowService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths
from app.storage.repositories import PortfolioRepository


async def run(position_id: str | None, portfolio_value: Decimal | None) -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    reference_value = portfolio_value or settings.portfolio_reference_value
    if reference_value is None:
        raise ValueError("set INVEST_PORTFOLIO_REFERENCE_VALUE or pass --portfolio-value")
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            selected = position_id
            if selected is None:
                positions = PortfolioRepository(
                    session, settings.portfolio_workspace_id
                ).list_positions()
                if not positions:
                    raise ValueError("no saved position is available")
                selected = positions[0].position_id
            decision, analysis, report = await AnalysisWorkflowService(
                session,
                settings.portfolio_workspace_id,
                settings,
            ).run(
                position_id=selected,
                portfolio_value=Money(amount=reference_value, currency="CNY"),
                now=datetime.now(UTC),
            )
    finally:
        database.dispose()
    print(f"decision: {decision.label}")
    print(f"analysis status: {analysis.status}")
    print(f"report id: {report.report_id}")
    print("advisory only: true")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--position-id")
    parser.add_argument("--portfolio-value", type=Decimal)
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.position_id, arguments.portfolio_value))


if __name__ == "__main__":
    raise SystemExit(main())
