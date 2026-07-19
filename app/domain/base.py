"""Shared immutable primitives for versioned domain contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"),
]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
UnitInterval = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1"))]
SignedRatio = Annotated[Decimal, Field(ge=Decimal("-1"), le=Decimal("1"))]


class DomainModel(BaseModel):
    """Base for immutable, reject-unknown domain values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    schema_version: Literal["1.0"] = SCHEMA_VERSION


class Money(DomainModel):
    """A non-negative monetary value with an explicit currency."""

    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class MoneyRange(DomainModel):
    """A non-binding amount range used only in advisory output."""

    minimum: Money
    maximum: Money

    @model_validator(mode="after")
    def validate_range(self) -> MoneyRange:
        if self.minimum.currency != self.maximum.currency:
            raise ValueError("money range currencies must match")
        if self.minimum.amount > self.maximum.amount:
            raise ValueError("minimum amount cannot exceed maximum amount")
        return self


class IdempotencyKey(DomainModel):
    """Stable key and payload digest required for mutating application operations."""

    scope: Identifier
    key: Identifier
    payload_sha256: Sha256
