"""Typed application configuration with secure defaults."""

from functools import lru_cache
from ipaddress import ip_address
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    database_url: str = "sqlite:///./data/investment_tool.db"
    allow_public_bind: bool = False
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "INVEST_DEEPSEEK_API_KEY"),
    )

    @model_validator(mode="after")
    def validate_security_defaults(self) -> "Settings":
        if self.environment == "production" and self.debug:
            raise ValueError("debug mode must be disabled in production")

        if not _is_loopback_host(self.host) and not self.allow_public_bind:
            raise ValueError(
                "non-loopback binding requires INVEST_ALLOW_PUBLIC_BIND=true and a security review"
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance for the current process."""

    return Settings()
