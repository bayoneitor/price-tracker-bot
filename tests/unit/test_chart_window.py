"""A chart shows a window of time, not the last N readings.

Ported from upstream bernalli/price-tracker-bot#32, against this fork's
`bot/charts.py` and its shared change-point query.

`price_history` records every *check*, not every *change*. Asking for the last
hundred rows therefore bought a window of `100 × check interval` — about four
days on a real deployment — and in four days almost nothing moves, so a product
whose price had stepped down three times still drew a flat line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot import charts
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _reading(days_ago: int, price: str) -> dict[str, Any]:
    return {"checked_at": (NOW - timedelta(days=days_ago)).isoformat(), "price": price}


def _db(points: list[dict[str, Any]]) -> AsyncMock:
    db = AsyncMock()
    db.get_price_change_points = AsyncMock(return_value={1: points})
    return db


async def _series(db: Any, **kwargs: Any) -> Any:
    """Render a chart and hand back the points it was asked to draw."""
    captured: dict[str, Any] = {}

    def spy(dates: list[Any], prices: list[float], target: Any, name: str, **_kw: Any) -> Any:
        captured["points"] = list(zip(dates, prices, strict=True))
        return charts._to_png(_blank_figure())

    original = charts._render_chart
    charts._render_chart = spy
    try:
        await charts.generate_chart(
            db, 1, {"id": 1, "name": "Widget", "domain": "example.com"}, **kwargs
        )
    finally:
        charts._render_chart = original
    return captured.get("points", [])


def _blank_figure() -> Any:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    return Figure(figsize=(1, 1))


@pytest.mark.asyncio
async def test_a_bounded_window_is_asked_for_in_time_not_in_rows() -> None:
    db = _db([_reading(80, "100"), _reading(1, "90")])

    await _series(db, days=charts.CHART_WINDOW_DAYS)

    kwargs = db.get_price_change_points.await_args.kwargs
    assert "since" in kwargs, "the chart must bound its query by date"
    since = datetime.strptime(kwargs["since"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    assert (datetime.now(UTC) - since).days == charts.CHART_WINDOW_DAYS


@pytest.mark.asyncio
async def test_the_default_window_is_everything_the_product_remembers() -> None:
    """A reader who wants less says so with the range buttons; the first
    picture shows the whole memory rather than guessing at a slice of it."""
    db = _db([_reading(800, "100"), _reading(1, "90")])

    await _series(db)

    assert db.get_price_change_points.await_args.kwargs["since"] is None


@pytest.mark.asyncio
async def test_a_change_older_than_a_hundred_readings_still_shows() -> None:
    """The whole point: the step down is 80 days back, far past 100 checks."""
    points = await _series(_db([_reading(80, "150"), _reading(40, "120"), _reading(1, "120")]))

    assert [price for _when, price in points] == [150.0, 120.0, 120.0]


@pytest.mark.asyncio
async def test_the_series_is_chronological() -> None:
    points = await _series(_db([_reading(1, "90"), _reading(30, "100"), _reading(60, "110")]))

    assert [when for when, _price in points] == sorted(when for when, _price in points)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-number", "", "-5", "0", "nan", "inf"])
async def test_a_corrupt_price_is_dropped_not_plotted(bad: str) -> None:
    """Plotted as zero it would drag the axis away from the real price range."""
    points = await _series(_db([_reading(30, "100"), _reading(20, bad), _reading(1, "90")]))

    assert [price for _when, price in points] == [100.0, 90.0]


@pytest.mark.asyncio
async def test_too_few_usable_points_draws_nothing() -> None:
    assert await charts.generate_chart(_db([_reading(1, "100")]), 1, {"id": 1}) is None


# ── Against a real repository, not a mock ────────────────────────────────


@pytest_asyncio.fixture
async def repo() -> Any:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_chart_works_against_a_real_repository(repo: Repository) -> None:
    """Every chart test hands the renderer a mock, which answers anything asked.

    Upstream shipped a chart that raised TypeError on every /history call and
    every alert because the query was reached for on the class and called
    unbound — and no mock-based test could see it.
    """
    product_id = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    for price in ("100", "100", "90"):
        await repo.add_price_history(product_id, Decimal(price))

    png = await charts.generate_chart(repo, product_id, {"id": product_id, "name": "Widget"})

    assert png is not None
    assert png.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
