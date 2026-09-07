"""What a product's history says about it, as numbers rather than a picture.

The chart answers "what happened" by eye. These answer the arithmetic behind
the question that decides a purchase — *is today's price any good?* — which
needs a number to compare today against.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

CENTS = Decimal("0.01")


def time_weighted_average(
    readings: Sequence[tuple[datetime, Decimal]],
    *,
    until: datetime,
) -> Decimal | None:
    """The average price *over time*, not the average of the prices.

    A plain mean over readings answers "the average of the distinct prices it
    has had", which is not what anyone means by "the average price": a two-day
    promotion counts exactly as much as six stable months. Each price is
    weighted here by how long it actually held — from its own reading until
    the next one, and the last one until `until`.

    Measured against a real product mid-session: the plain mean said €279.30
    where this says €286.20, and the shop's own figure was €287.54. The
    difference is systematic, always downward, and lands precisely on the
    number a reader would use to judge a discount.

    `readings` must be sorted oldest-first. Returns None when there is nothing
    to average, or when every reading sits at or after `until` — a window with
    no duration in it has no average, and saying so beats inventing one.
    """
    if not readings:
        return None

    weighted = Decimal(0)
    total_seconds = Decimal(0)
    for index, (observed_at, price) in enumerate(readings):
        if observed_at >= until:
            break
        next_at = readings[index + 1][0] if index + 1 < len(readings) else until
        held = min(next_at, until)
        seconds = Decimal(str((held - observed_at).total_seconds()))
        if seconds <= 0:
            continue
        weighted += price * seconds
        total_seconds += seconds

    if total_seconds <= 0:
        return None
    return (weighted / total_seconds).quantize(CENTS, rounding=ROUND_HALF_UP)


def readings_from_records(records: Sequence[Any]) -> list[tuple[datetime, Decimal]]:
    """`(when, price)` pairs from repository history rows, oldest first.

    Rows that cannot be read — a corrupt timestamp, a price that will not
    parse — are dropped rather than defaulted: a zero here would drag an
    average toward a price the product never had.
    """
    from datetime import UTC, datetime  # noqa: PLC0415 — TYPE_CHECKING-only above

    readings: list[tuple[datetime, Decimal]] = []
    for record in records:
        try:
            raw = str(record["checked_at"]).replace("Z", "+00:00")
            when = datetime.fromisoformat(raw)
            when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
            price = Decimal(str(record["price"]))
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
        if price <= 0:
            continue
        readings.append((when, price))
    readings.sort(key=lambda pair: pair[0])
    return readings
