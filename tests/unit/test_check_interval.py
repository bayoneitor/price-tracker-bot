"""A product's own check interval must actually be honoured.

`/refresh 3 1440` wrote `products.check_interval_minutes`, the listing card
rendered "🔄 Check: every 24 hours", and the sweep checked the product on every
run regardless: nothing in `core/` ever read the column. The bot reported a
setting it did not apply.

The interval is a *minimum gap*. It cannot make a product checked more often than
the sweep itself runs, so a value below the sweep means "every run" — and the
command says so rather than letting the number imply otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from price_tracker.core.scheduler import Scheduler, SchedulerDeps, _last_checked


def _scheduler() -> Scheduler:
    return Scheduler(
        SchedulerDeps(
            repo=MagicMock(), registry=MagicMock(), client=MagicMock(), notifier=MagicMock()
        )
    )


def _product(interval: int | None, *, checked_minutes_ago: float | None) -> Any:
    last = (
        None
        if checked_minutes_ago is None
        else (datetime.now(UTC) - timedelta(minutes=checked_minutes_ago)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )
    return MagicMock(id=1, check_interval_minutes=interval, last_checked_at=last)


@pytest.mark.parametrize(
    ("interval", "minutes_ago", "due"),
    [
        (60, 90, True),  # its hour has passed
        (60, 30, False),  # checked half an hour ago: not yet
        (60, 60, True),  # exactly due counts as due
        (1440, 300, False),  # a daily product on a six-hourly sweep waits
        (None, 1, True),  # no interval of its own: rides every sweep
        (0, 1, True),  # cleared back to the global one
        (60, None, True),  # never checked
    ],
)
def test_a_product_is_due_only_once_its_interval_has_passed(
    interval: int | None, minutes_ago: float | None, due: bool
) -> None:
    assert _scheduler()._is_due(_product(interval, checked_minutes_ago=minutes_ago)) is due


def test_an_unreadable_timestamp_does_not_strand_a_product() -> None:
    """Skipping forever on a bad row would be worse than one early check."""
    assert _scheduler()._is_due(MagicMock(check_interval_minutes=60, last_checked_at="whenever"))


def test_a_naive_timestamp_is_read_as_utc_not_as_local_time() -> None:
    """SQLite writes them naive; reading them as local would shift every gap."""
    parsed = _last_checked("2026-09-01 10:00:00")

    assert parsed == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_an_unusable_timestamp_parses_to_none(value: str | None) -> None:
    assert _last_checked(value) is None


# ── The sweep skips what is not due ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_scrapes_only_the_products_that_are_due() -> None:
    from price_tracker.core.notices import NoticeCollector

    scheduler = _scheduler()
    scheduler.deps.delay_between_products = 0
    scraped: list[int] = []

    async def spy(product: Any, *, collector: Any) -> None:
        scraped.append(product.id)

    scheduler._scrape_one = spy  # type: ignore[method-assign]

    due = _product(60, checked_minutes_ago=90)
    due.id = 1
    due.url = "https://example.com/p/1"
    not_due = _product(60, checked_minutes_ago=5)
    not_due.id = 2
    not_due.url = "https://example.com/p/2"
    no_interval = _product(None, checked_minutes_ago=1)
    no_interval.id = 3
    no_interval.url = "https://example.com/p/3"

    await scheduler._run_tick([due, not_due, no_interval], collector=NoticeCollector())

    assert scraped == [1, 3]


# ── The caveat, where the number cannot mean what it says ────────────────


@pytest.mark.parametrize(
    ("minutes", "sweep", "expected"),
    [(30, 360, True), (360, 360, False), (720, 360, False)],
)
def test_an_interval_below_the_sweep_says_so(minutes: int, sweep: int, expected: bool) -> None:
    from price_tracker.bot.handlers.monitoring import _interval_caveat

    assert bool(_interval_caveat(minutes, sweep)) is expected
