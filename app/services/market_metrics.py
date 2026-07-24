"""Deterministic, split-aware market metrics; AI must never estimate these values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import sqrt
from statistics import stdev

from app.domain.portfolio import MarketSnapshot

_EIGHT_PLACES = Decimal("0.00000001")
_CHINA_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")
_FACTSHEET_DATE = re.compile(r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
_SAMPLE_COUNT = re.compile(r"样本股数\s*(?P<count>\d{1,5})")


@dataclass(frozen=True)
class MarketPoint:
    as_of: datetime
    price: Decimal
    split_adjustment_factor: Decimal = Decimal("1")
    volume: Decimal | None = None

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("market point timestamps must include a timezone")
        if self.price <= 0 or self.split_adjustment_factor <= 0:
            raise ValueError("market prices and split factors must be positive")
        if self.volume is not None and self.volume < 0:
            raise ValueError("market volume cannot be negative")

    @property
    def adjusted_price(self) -> Decimal:
        return self.price * self.split_adjustment_factor


def calculate_market_snapshot(
    *,
    asset_id: str,
    code: str,
    source_id: str,
    points: tuple[MarketPoint, ...],
    net_asset_value: Decimal | None = None,
    price_earnings_ratio: Decimal | None = None,
    price_book_ratio: Decimal | None = None,
) -> MarketSnapshot:
    if not points:
        raise ValueError("at least one market point is required")
    ordered = tuple(sorted(points, key=lambda item: item.as_of))
    if len({point.as_of for point in ordered}) != len(ordered):
        raise ValueError("market point timestamps must be unique")

    adjusted = tuple(point.adjusted_price for point in ordered)
    returns = tuple(
        (current / previous) - Decimal("1")
        for previous, current in zip(adjusted, adjusted[1:], strict=False)
    )
    daily_change = returns[-1] if returns else None
    period_return = (adjusted[-1] / adjusted[0]) - Decimal("1") if len(adjusted) > 1 else None
    volatility = None
    if len(returns) >= 2:
        volatility = Decimal(str(stdev(float(value) for value in returns) * sqrt(252))).quantize(
            _EIGHT_PLACES
        )

    peak = adjusted[0]
    maximum_drawdown = Decimal("0")
    for price in adjusted:
        peak = max(peak, price)
        drawdown = (price / peak) - Decimal("1")
        maximum_drawdown = min(maximum_drawdown, drawdown)

    latest = ordered[-1]
    return MarketSnapshot(
        asset_id=asset_id,
        code=code,
        as_of=latest.as_of,
        net_asset_value=net_asset_value,
        close_price=latest.price,
        volume=latest.volume,
        daily_change_ratio=daily_change,
        period_return_ratio=period_return,
        price_earnings_ratio=price_earnings_ratio,
        price_book_ratio=price_book_ratio,
        volatility_ratio=volatility,
        maximum_drawdown_ratio=maximum_drawdown,
        split_adjustment_factor=latest.split_adjustment_factor,
        source_id=source_id,
    )


def extract_h30184_factsheet_snapshot(
    text: str,
    *,
    asset_id: str = "asset-007300",
    source_id: str = "csi-h30184-factsheet",
) -> MarketSnapshot | None:
    """Extract only unambiguous fields from the official H30184 fact sheet text layer."""

    if "H30184" not in text or "中证全指半导体产品与设备指数" not in text:
        return None
    sample_match = _SAMPLE_COUNT.search(text)
    dates = [
        datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=_CHINA_TIME,
        )
        for match in _FACTSHEET_DATE.finditer(text)
    ]
    if sample_match is None or not dates:
        return None
    return MarketSnapshot(
        asset_id=asset_id,
        code="H30184",
        as_of=max(dates),
        sample_count=int(sample_match.group("count")),
        source_id=source_id,
    )
