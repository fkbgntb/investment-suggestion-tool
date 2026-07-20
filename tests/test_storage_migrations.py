from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.storage.migrations import downgrade_database, upgrade_database
from app.storage.models import Base

EXPECTED_TABLES = {
    "active_taxonomy_configurations",
    "ai_extraction_runs",
    "alembic_version",
    "analysis_runs",
    "assets",
    "audit_events",
    "crawl_runs",
    "decision_results",
    "entities",
    "event_clusters",
    "evidence_items",
    "evidence_scores",
    "exposures",
    "idempotency_records",
    "human_relevance_labels",
    "investment_profiles",
    "normalized_documents",
    "positions",
    "position_snapshots",
    "raw_documents",
    "reports",
    "relevance_assessments",
    "scheduled_tasks",
    "scheduler_states",
    "source_adapter_states",
    "source_health",
    "sources",
    "taxonomy_configurations",
    "topics",
    "workspaces",
}


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def revision(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def test_empty_database_upgrades_and_can_round_trip_old_revision(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "migration.sqlite3")

    upgrade_database(database_url)
    engine = create_engine(database_url)
    try:
        assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
        columns = {column["name"] for column in inspect(engine).get_columns("workspaces")}
        assert "raw_document_retention_days" in columns
    finally:
        engine.dispose()
    assert revision(database_url) == "0010"

    downgrade_database(database_url, "0001")
    assert revision(database_url) == "0001"
    upgrade_database(database_url)
    assert revision(database_url) == "0010"


def test_version_one_database_with_existing_rows_upgrades_safely(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "old.sqlite3")
    upgrade_database(database_url, "0001")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces "
                    "(workspace_id, name, deleted_at, created_at, updated_at) "
                    "VALUES ('legacy', 'Legacy', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    upgrade_database(database_url)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT raw_document_retention_days FROM workspaces "
                        "WHERE workspace_id='legacy'"
                    )
                )
                == 90
            )
    finally:
        engine.dispose()


def test_schema_compiles_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in ddl
