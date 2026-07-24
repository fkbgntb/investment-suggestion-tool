"""Typed origin semantics for discovered and verified information."""

from __future__ import annotations

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from app.domain.base import DomainModel, Identifier
from app.domain.enums import ContentType


class OriginProvenance(DomainModel):
    discovery_source_id: Identifier
    original_publisher: str = Field(min_length=1, max_length=300)
    original_domain: str = Field(min_length=1, max_length=253)
    original_url: AnyHttpUrl
    verified_original: bool = False
    content_type: ContentType

    @field_validator("original_domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        normalized = value.strip().rstrip(".").encode("idna").decode("ascii").casefold()
        if not normalized or "/" in normalized or ":" in normalized or "*" in normalized:
            raise ValueError("original domain must be an explicit hostname")
        return normalized

    @model_validator(mode="after")
    def verified_origin_must_match_url_host(self) -> OriginProvenance:
        url_host = (self.original_url.host or "").encode("idna").decode("ascii").casefold()
        if self.verified_original and url_host != self.original_domain:
            raise ValueError("a verified original URL must match its original domain")
        return self
