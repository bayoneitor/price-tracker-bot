"""A backfilled product's chart: two colours, a tracking-start line, a wider window.

`generate_chart` decides all three from `product["history_source"]` alone —
set once, at backfill time (`add_price_history_bulk`), and never touched
again — so a product that was never backfilled asks exactly what it always
asked and draws in exactly the one colour it always drew.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from price_tracker.bot import charts

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _reading(days_ago: int, price: str, *, source: str | None = None) -> dict[str, Any]:
    return {
        "checked_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "price": price,
        "source": source,
    }


def _db(points: list[dict[str, Any]]) -> AsyncMock:
    db = AsyncMock()
    db.get_price_change_points = AsyncMock(return_value={1: points})
    return db


async def _rendered(db: Any, product: dict[str, Any]) -> dict[str, Any]:
    """Render a chart and hand back what `_render_chart` was actually called with."""
    captured: dict[str, Any] = {}
    real_render = charts._render_chart

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_render(*args, **kwargs)

    original = charts._render_chart
    charts._render_chart = spy
    try:
        await charts.generate_chart(db, 1, product)
    finally:
        charts._render_chart = original
    return captured


# ── Untouched by default ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_product_with_no_backfill_asks_the_usual_window() -> None:
    db = _db([_reading(80, "100"), _reading(1, "90")])

    kwargs = (await _rendered(db, {"id": 1, "name": "Widget"}))["kwargs"]

    call_kwargs = db.get_price_change_points.await_args.kwargs
    since = datetime.strptime(call_kwargs["since"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    assert (datetime.now(UTC) - since).days == charts.CHART_WINDOW_DAYS
    assert kwargs["tracking_started_at"] is None


@pytest.mark.asyncio
async def test_a_product_with_no_backfill_draws_no_boundary() -> None:
    """Every point unsourced — the whole point stays the one accent colour."""
    db = _db([_reading(80, "100"), _reading(1, "90")])

    kwargs = (await _rendered(db, {"id": 1, "name": "Widget"}))["kwargs"]

    assert kwargs["sources"] == [None, None]


# ── The wider window ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_backfilled_product_asks_the_wider_window() -> None:
    db = _db([_reading(500, "100", source="keepa"), _reading(1, "90")])
    product = {"id": 1, "name": "Widget", "history_source": "keepa", "created_at": None}

    await _rendered(db, product)

    call_kwargs = db.get_price_change_points.await_args.kwargs
    assert call_kwargs["since"] is None
    assert call_kwargs["limit_per_product"] == charts.CHART_MAX_ROWS


@pytest.mark.asyncio
async def test_a_backfilled_product_still_draws_within_the_default_window() -> None:
    """A wider window is opened; nothing forces the chart to fill it."""
    db = _db([_reading(80, "100", source="keepa"), _reading(1, "90")])
    product = {"id": 1, "name": "Widget", "history_source": "keepa", "created_at": None}

    result = await charts.generate_chart(db, 1, product)

    assert result is not None


# ── The boundary and the two colours ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_split_lands_between_the_last_imported_and_first_live_point() -> None:
    db = _db(
        [
            _reading(80, "100", source="keepa"),
            _reading(60, "95", source="keepa"),
            _reading(30, "90"),
            _reading(1, "85"),
        ]
    )
    product = {"id": 1, "name": "Widget", "history_source": "keepa", "created_at": None}

    kwargs = (await _rendered(db, product))["kwargs"]

    assert kwargs["sources"] == ["keepa", "keepa", None, None]


@pytest.mark.asyncio
async def test_all_imported_points_draw_one_colour_when_no_live_check_has_run_yet() -> None:
    """Added moments ago, before the first scheduled check — every point is
    imported, and the chart must not divide-by-zero looking for a boundary."""
    db = _db([_reading(80, "100", source="keepa"), _reading(60, "95", source="keepa")])
    product = {"id": 1, "name": "Widget", "history_source": "keepa", "created_at": None}

    result = await charts.generate_chart(db, 1, product)

    assert result is not None


# ── The tracking-start line ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tracking_start_line_uses_the_products_own_created_at() -> None:
    db = _db([_reading(80, "100", source="keepa"), _reading(1, "90")])
    product = {
        "id": 1,
        "name": "Widget",
        "history_source": "keepa",
        "created_at": "2026-06-15 00:00:00",
    }

    kwargs = (await _rendered(db, product))["kwargs"]

    assert kwargs["tracking_started_at"] == datetime(2026, 6, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_no_line_without_a_backfill_even_if_created_at_is_set() -> None:
    """`created_at` exists on every product; the line is not drawn from that alone."""
    db = _db([_reading(80, "100"), _reading(1, "90")])
    product = {"id": 1, "name": "Widget", "created_at": "2026-06-15 00:00:00"}

    kwargs = (await _rendered(db, product))["kwargs"]

    assert kwargs["tracking_started_at"] is None


@pytest.mark.asyncio
async def test_an_unparseable_created_at_draws_no_line_rather_than_raising() -> None:
    db = _db([_reading(80, "100", source="keepa"), _reading(1, "90")])
    product = {
        "id": 1,
        "name": "Widget",
        "history_source": "keepa",
        "created_at": "not-a-timestamp",
    }

    result = await charts.generate_chart(db, 1, product)

    assert result is not None
