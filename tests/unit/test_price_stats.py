"""`time_weighted_average` — the average price *over time*, not of the prices.

A plain mean over readings answers "the average of the distinct prices it has
had", which is not what anyone means by "the average price": a two-day
promotion counts exactly as much as six stable months. Measured against a real
tracked product mid-session, the plain mean said €279.30 where the weighted
one said €286.20, and the shop's own figure was €287.54.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from price_tracker.core.price_stats import readings_from_records, time_weighted_average

UNTIL = datetime(2026, 2, 1, tzinfo=UTC)


def _at(day: int, price: str) -> tuple[datetime, Decimal]:
    return datetime(2026, 1, day, tzinfo=UTC), Decimal(price)


# ── The weighting itself ─────────────────────────────────────────────────


def test_a_single_price_held_the_whole_window_is_that_price() -> None:
    assert time_weighted_average([_at(1, "100")], until=UNTIL) == Decimal("100.00")


def test_a_price_is_weighted_by_how_long_it_held() -> None:
    """30 days at 100 then 1 day at 50 is not an average of 75."""
    readings = [_at(1, "100"), _at(31, "50")]

    average = time_weighted_average(readings, until=UNTIL)

    assert average == Decimal("98.39")  # (30*100 + 1*50) / 31


def test_the_plain_mean_of_the_same_readings_would_say_something_else() -> None:
    """The whole reason this function exists rather than `sum() / len()`."""
    readings = [_at(1, "100"), _at(31, "50")]
    plain = sum(price for _when, price in readings) / len(readings)

    assert plain == Decimal("75")
    assert time_weighted_average(readings, until=UNTIL) == Decimal("98.39")


def test_the_last_price_holds_until_the_end_of_the_window() -> None:
    """Not until the last reading — the price did not stop existing then."""
    half_way = datetime(2026, 1, 21, tzinfo=UTC)
    readings = [_at(1, "100"), _at(11, "200")]

    # 10 days at 100, then 10 days at 200 up to `half_way`.
    assert time_weighted_average(readings, until=half_way) == Decimal("150.00")


def test_rounding_is_half_up_to_the_cent() -> None:
    readings = [_at(1, "10.005")]

    assert time_weighted_average(readings, until=UNTIL) == Decimal("10.01")


# ── Nothing to average ───────────────────────────────────────────────────


def test_no_readings_is_none_not_zero() -> None:
    assert time_weighted_average([], until=UNTIL) is None


def test_readings_entirely_after_the_window_are_none() -> None:
    """A window with no duration in it has no average; saying so beats
    inventing one."""
    after = [(UNTIL + timedelta(days=1), Decimal("100"))]

    assert time_weighted_average(after, until=UNTIL) is None


def test_a_reading_exactly_at_the_window_end_carries_no_weight() -> None:
    assert time_weighted_average([(UNTIL, Decimal("100"))], until=UNTIL) is None


# ── Reading repository rows ──────────────────────────────────────────────


def test_records_become_pairs_oldest_first() -> None:
    records = [
        {"checked_at": "2026-01-11T00:00:00Z", "price": "200"},
        {"checked_at": "2026-01-01T00:00:00Z", "price": "100"},
    ]

    readings = readings_from_records(records)

    assert [price for _when, price in readings] == [Decimal("100"), Decimal("200")]


def test_a_naive_timestamp_is_read_as_utc() -> None:
    readings = readings_from_records([{"checked_at": "2026-01-01 00:00:00", "price": "100"}])

    assert readings[0][0] == datetime(2026, 1, 1, tzinfo=UTC)


def test_an_unreadable_row_is_dropped_not_defaulted() -> None:
    """A zero here would drag the average toward a price the product never had."""
    records = [
        {"checked_at": "2026-01-01T00:00:00Z", "price": "100"},
        {"checked_at": "not a date", "price": "100"},
        {"checked_at": "2026-01-02T00:00:00Z", "price": "not a number"},
        {"checked_at": "2026-01-03T00:00:00Z", "price": "0"},
        {"checked_at": "2026-01-04T00:00:00Z", "price": "-5"},
        {"price": "100"},
    ]

    assert len(readings_from_records(records)) == 1


def test_the_two_compose_into_an_average_over_real_rows() -> None:
    records = [
        {"checked_at": "2026-01-01T00:00:00Z", "price": "100"},
        {"checked_at": "2026-01-31T00:00:00Z", "price": "50"},
    ]

    average = time_weighted_average(readings_from_records(records), until=UNTIL)

    assert average == Decimal("98.39")


# ── `recent_averages`: one query for a whole page ────────────────────────


async def test_one_query_covers_every_product_on_the_page() -> None:
    """Six round trips to draw one screen is how a listing gets slow."""
    from unittest.mock import AsyncMock

    from price_tracker.bot.handlers.product_list import recent_averages

    db = AsyncMock()
    db.get_price_change_points = AsyncMock(return_value={})

    await recent_averages(db, [1, 2, 3, 4, 5, 6])

    db.get_price_change_points.assert_awaited_once()
    assert db.get_price_change_points.await_args.args[0] == [1, 2, 3, 4, 5, 6]


async def test_no_products_asks_nothing_at_all() -> None:
    from unittest.mock import AsyncMock

    from price_tracker.bot.handlers.product_list import recent_averages

    db = AsyncMock()

    assert await recent_averages(db, []) == {}
    db.get_price_change_points.assert_not_awaited()


async def test_a_repository_that_fails_costs_the_average_not_the_listing() -> None:
    """ "Never raises" has to mean the whole computation, not just the query —
    a repository handing back an unexpected shape would otherwise take the
    listing down with it."""
    from unittest.mock import AsyncMock

    from price_tracker.bot.handlers.product_list import recent_averages

    exploding = AsyncMock()
    exploding.get_price_change_points = AsyncMock(side_effect=RuntimeError("boom"))
    assert await recent_averages(exploding, [1]) == {}

    nonsense = AsyncMock()
    nonsense.get_price_change_points = AsyncMock(return_value="not a mapping")
    assert await recent_averages(nonsense, [1]) == {}


async def test_products_with_history_get_an_average_keyed_by_id() -> None:
    from unittest.mock import AsyncMock

    from price_tracker.bot.handlers.product_list import recent_averages

    db = AsyncMock()
    db.get_price_change_points = AsyncMock(
        return_value={
            1: [{"checked_at": "2026-01-01T00:00:00Z", "price": "100"}],
            2: [],  # tracked, but nothing in the window
        }
    )

    averages = await recent_averages(db, [1, 2])

    assert averages[1] == Decimal("100.00")
    assert 2 not in averages
