"""Data-only policies and sanitized status contracts for outbound collection."""

from __future__ import annotations

import re

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.enums import FetchErrorCode, SourceHealthStatus

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


class URLPolicy(DomainModel):
    """Per-source outbound policy; adapters cannot override these limits."""

    source_id: Identifier
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=100)
    allow_subdomains: bool = False
    allowed_schemes: tuple[str, ...] = ("https",)
    allowed_ports: tuple[int, ...] = Field(default=(443,), min_length=1, max_length=20)
    allowed_content_types: tuple[str, ...] = Field(
        default=("application/json", "application/xml", "text/html", "text/plain"),
        min_length=1,
        max_length=50,
    )
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=10_000_000)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    read_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    total_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    minimum_interval_seconds: float = Field(default=1.0, ge=0, le=3_600)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_cooldown_seconds: float = Field(default=300.0, gt=0, le=86_400)
    user_agent: str = Field(
        default="investment-suggestion-tool/0.1 (personal research)",
        min_length=8,
        max_length=300,
    )

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            host = value.strip().rstrip(".").encode("idna").decode("ascii").casefold()
            if not host or "*" in host:
                raise ValueError("allowed hosts must be explicit hostnames without wildcards")
            labels = host.split(".")
            if any(not _HOST_LABEL.fullmatch(label) for label in labels):
                raise ValueError("allowed host is invalid")
            normalized.append(host)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed hosts must be unique")
        return tuple(normalized)

    @field_validator("allowed_schemes")
    @classmethod
    def validate_schemes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.casefold() for value in values)
        if not normalized or any(value not in {"http", "https"} for value in normalized):
            raise ValueError("only explicit HTTP and HTTPS schemes are supported")
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed schemes must be unique")
        return normalized

    @field_validator("allowed_ports")
    @classmethod
    def validate_ports(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(port < 1 or port > 65_535 for port in values):
            raise ValueError("allowed ports must be between 1 and 65535")
        if len(set(values)) != len(values):
            raise ValueError("allowed ports must be unique")
        return values

    @field_validator("allowed_content_types")
    @classmethod
    def normalize_content_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().casefold() for value in values)
        if any(not _CONTENT_TYPE.fullmatch(value) for value in normalized):
            raise ValueError("allowed content types must be concrete MIME types")
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed content types must be unique")
        return normalized

    @field_validator("user_agent")
    @classmethod
    def reject_header_injection(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("user agent cannot contain line breaks")
        return value

    @model_validator(mode="after")
    def validate_timeout_budget(self) -> URLPolicy:
        if self.total_timeout_seconds < max(
            self.connect_timeout_seconds, self.read_timeout_seconds
        ):
            raise ValueError("total timeout cannot be shorter than component timeouts")
        return self


class FetchFailure(DomainModel):
    """Failure data safe to persist in CrawlRun; never contains a URL or resolved address."""

    source_id: Identifier
    error_code: FetchErrorCode
    retryable: bool
    occurred_at: AwareDatetime


class SourceHealthSnapshot(DomainModel):
    source_id: Identifier
    status: SourceHealthStatus
    consecutive_failures: int = Field(ge=0)
    last_error_code: FetchErrorCode | None = None
    last_success_at: AwareDatetime | None = None
    last_failure_at: AwareDatetime | None = None
    circuit_open_until: AwareDatetime | None = None


class SourceAdapterState(DomainModel):
    """Versioned, adapter-owned cursor state safe for optimistic updates."""

    source_id: Identifier
    adapter_name: Identifier
    adapter_version: str = Field(min_length=1, max_length=64)
    state_version: int = Field(default=0, ge=0)
    cursor: str | None = Field(default=None, max_length=2_000)
    updated_at: AwareDatetime


class SourceOperationalStatus(DomainModel):
    source_id: Identifier
    enabled: bool
    health: SourceHealthSnapshot
    is_stale: bool
    stale_after_hours: int = Field(default=8, ge=1, le=168)
