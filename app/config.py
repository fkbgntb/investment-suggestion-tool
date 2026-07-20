"""Typed application configuration with secure defaults."""

import os
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app import __version__


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True

    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Settings loaded from environment variables and an optional local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="INVEST_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "ETF 投资分析工具"
    app_version: str = __version__
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/investment_tool.db"
    portfolio_workspace_id: str = Field(
        default="personal-demo",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    raw_document_retention_days: int = Field(default=90, ge=1, le=3650)
    require_external_data_dir: bool = False
    allow_public_bind: bool = False
    collector_proxy_url: AnyHttpUrl | None = None
    gdelt_max_records: int = Field(default=50, ge=1, le=250)
    gdelt_max_documents_per_day: int = Field(default=500, ge=1, le=10_000)
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "INVEST_DEEPSEEK_API_KEY"),
    )
    backup_passphrase: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "INVEST_BACKUP_PASSPHRASE",
            "BACKUP_PASSPHRASE",
        ),
    )

    @model_validator(mode="after")
    def validate_security_defaults(self) -> "Settings":
        if self.environment == "production" and self.debug:
            raise ValueError("debug mode must be disabled in production")

        if not _is_loopback_host(self.host) and not self.allow_public_bind:
            raise ValueError(
                "non-loopback binding requires INVEST_ALLOW_PUBLIC_BIND=true and a security review"
            )

        resolved_data_dir = self.data_dir.expanduser().resolve()
        if self.require_external_data_dir:
            system_drive = os.environ.get("SYSTEMDRIVE", "C:")
            if resolved_data_dir.drive.casefold() == Path(system_drive).drive.casefold():
                raise ValueError("data directory must not be located on the system drive")

        try:
            database_url = make_url(self.database_url)
        except ArgumentError as error:
            raise ValueError("database URL is invalid") from error
        if (
            database_url.get_backend_name() == "sqlite"
            and database_url.database
            and database_url.database != ":memory:"
        ):
            database_path = Path(database_url.database).expanduser().resolve()
            if not database_path.is_relative_to(resolved_data_dir):
                raise ValueError("SQLite database must be located inside INVEST_DATA_DIR")

        if (
            self.backup_passphrase is not None
            and len(self.backup_passphrase.get_secret_value()) < 16
        ):
            raise ValueError("backup passphrase must contain at least 16 characters")

        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance for the current process."""

    return Settings()
