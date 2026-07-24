"""Typed application configuration with secure defaults."""

import os
from decimal import Decimal
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator, model_validator
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
    portfolio_reference_value: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    raw_document_retention_days: int = Field(default=90, ge=1, le=3650)
    require_external_data_dir: bool = False
    allow_public_bind: bool = False
    collector_proxy_url: AnyHttpUrl | None = None
    gdelt_max_records: int = Field(default=50, ge=1, le=250)
    gdelt_max_documents_per_day: int = Field(default=500, ge=1, le=10_000)
    alpha_vantage_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "INVEST_ALPHA_VANTAGE_API_KEY",
            "ALPHA_VANTAGE_API_KEY",
        ),
    )
    alpha_vantage_max_records: int = Field(default=50, ge=1, le=1_000)
    alpha_vantage_max_calls_per_day: int = Field(default=20, ge=1, le=25)
    alpha_vantage_max_documents_per_day: int = Field(default=500, ge=1, le=10_000)
    sec_contact_email: str | None = Field(default=None, max_length=254, repr=False)
    sec_max_filings_per_company: int = Field(default=50, ge=1, le=250)
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "INVEST_DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: AnyHttpUrl = "https://api.deepseek.com"
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    deepseek_max_input_characters: int = Field(default=12_000, ge=1_000, le=100_000)
    deepseek_max_output_tokens: int = Field(default=1_200, ge=100, le=8_000)
    deepseek_max_calls_per_day: int = Field(default=20, ge=1, le=1_000)
    # Separate extraction and synthesis token guards let a local deployment allocate
    # one total spending envelope without weakening either output-size boundary.
    deepseek_daily_token_budget: int = Field(default=100_000, ge=1_000, le=10_000_000)
    deepseek_synthesis_max_calls_per_day: int = Field(default=10, ge=1, le=1_000)
    deepseek_synthesis_daily_token_budget: int = Field(default=50_000, ge=1_000, le=10_000_000)
    deepseek_synthesis_max_output_tokens: int = Field(default=2_400, ge=500, le=8_000)
    deepseek_timeout_seconds: float = Field(default=90, ge=10, le=300)
    backup_passphrase: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "INVEST_BACKUP_PASSPHRASE",
            "BACKUP_PASSPHRASE",
        ),
    )

    @field_validator("sec_contact_email")
    @classmethod
    def validate_sec_contact_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if (
            normalized.count("@") != 1
            or normalized.startswith("@")
            or normalized.endswith("@")
            or "." not in normalized.rsplit("@", 1)[1]
        ):
            raise ValueError("SEC contact email is invalid")
        return normalized

    @field_validator("deepseek_api_key", mode="before")
    @classmethod
    def empty_deepseek_key_is_not_configured(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("alpha_vantage_api_key", mode="before")
    @classmethod
    def empty_alpha_vantage_key_is_not_configured(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def validate_security_defaults(self) -> "Settings":
        if self.environment == "production" and self.debug:
            raise ValueError("debug mode must be disabled in production")

        if not _is_loopback_host(self.host) and not self.allow_public_bind:
            raise ValueError(
                "non-loopback binding requires INVEST_ALLOW_PUBLIC_BIND=true and a security review"
            )

        proxy_url = self.collector_proxy_url
        if proxy_url is not None and (
            proxy_url.scheme != "http"
            or proxy_url.host is None
            or not _is_loopback_host(proxy_url.host)
            or proxy_url.port is None
            or proxy_url.username is not None
            or proxy_url.password is not None
            or proxy_url.path not in {None, "", "/"}
            or proxy_url.query is not None
            or proxy_url.fragment is not None
        ):
            raise ValueError(
                "collector proxy must be a credential-free local HTTP endpoint with a port"
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

        deepseek_url = self.deepseek_base_url
        if (
            deepseek_url.scheme != "https"
            or deepseek_url.host != "api.deepseek.com"
            or deepseek_url.username is not None
            or deepseek_url.password is not None
            or deepseek_url.path not in {None, "", "/"}
            or deepseek_url.query is not None
            or deepseek_url.fragment is not None
        ):
            raise ValueError("DeepSeek API URL must be the credential-free official HTTPS host")

        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance for the current process."""

    return Settings()
