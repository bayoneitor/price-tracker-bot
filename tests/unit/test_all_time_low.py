"""`all_time_low` — a fifth `ThresholdType` fired against `products.lowest_price`,
not against `old`/`new` the way every other trigger is.

Evaluated in `Scheduler._check_product_core`, which already writes the new
price to `products.lowest_price` (via `update_price`) before this check runs
— the ordering `crosses_threshold` never has to worry about, because it only
ever sees the two prices passed to it. The critical property is that the
comparison still uses the *old* floor: `p`, fetched once at the top of the
call, never re-reads the row `update_price` just rewrote.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.core.alert import format_all_time_low
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


class _StubScraper(AbstractScraper):
    name = "stub"
    priority = 100

    def __init__(self, price: Decimal) -> None:
        self._price = price

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="Widget", price=self._price, currency="EUR")


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    r = Repository(conn)
    await r.ensure_user(user_id=1)
    try:
        yield r
    finally:
        await conn.close()


async def _tracked(repo: Repository, *, floor: Decimal) -> int:
    """A product whose lowest price on record is `floor`, tracking all_time_low."""
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=floor,
        currency="EUR",
    )
    await repo.set_threshold(pid, "all_time_low", Decimal("0"))
    return pid


async def _check(repo: Repository, price: Decimal) -> AsyncMock:
    """Run one sweep at `price`, returning the notifier mock so tests can inspect it."""
    registry = ScraperRegistry()
    registry.register(_StubScraper(price))
    notifier = AsyncMock(return_value=True)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                lang="en",
                delay_between_products=0.0,
            )
        )
        await scheduler.run_check_for_user(user_id=1)
    return notifier


# ── The trigger itself ───────────────────────────────────────────────────


async def test_a_genuine_new_low_fires() -> None:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    try:
        pid = await _tracked(repo, floor=Decimal("100"))
        notifier = await _check(repo, Decimal("80"))

        assert notifier.await_args is not None
        text = notifier.await_args.args[1]
        assert "All-time low" in text
        product = await repo.get_product(pid)
        assert product is not None
        assert product.lowest_price == Decimal("80")
    finally:
        await conn.close()


async def test_a_price_still_above_the_floor_does_not_fire() -> None:
    """A drop that is not a new low: the floor was pushed down to 70 by an
    earlier check, and 85 — though a drop from where it sits now — never
    beats that."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    try:
        pid = await _tracked(repo, floor=Decimal("100"))
        await repo.update_price(pid, Decimal("70"))  # an earlier check set the floor
        notifier = await _check(repo, Decimal("85"))

        notifier.assert_not_awaited()
    finally:
        await conn.close()


async def test_matching_the_floor_exactly_does_not_fire() -> None:
    """A new low means *below*, not "at least as good as"."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    try:
        await _tracked(repo, floor=Decimal("100"))
        notifier = await _check(repo, Decimal("100"))

        notifier.assert_not_awaited()
    finally:
        await conn.close()


async def test_a_rise_does_not_fire() -> None:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    try:
        await _tracked(repo, floor=Decimal("100"))
        notifier = await _check(repo, Decimal("120"))

        notifier.assert_not_awaited()
    finally:
        await conn.close()


async def test_a_product_on_a_different_threshold_type_never_fires_this_one() -> None:
    """Only a product that opted into `all_time_low` is evaluated against it."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    try:
        pid = await repo.add_product(
            user_id=1,
            url="https://example.com/p/1",
            name="Widget",
            domain="example.com",
            initial_price=Decimal("100"),
            currency="EUR",
        )
        await repo.set_threshold(pid, "percentage", Decimal("10"))  # not all_time_low
        notifier = await _check(repo, Decimal("50"))  # a huge drop, but wrong type

        # A 50% drop crosses the 10% percentage threshold too — this proves the
        # *reason* rather than whether anything fired at all.
        assert notifier.await_args is not None
        assert "All-time low" not in notifier.await_args.args[1]
    finally:
        await conn.close()


# ── The ordering the test must pin ───────────────────────────────────────


async def test_the_new_low_beats_the_old_floor_not_the_one_it_just_became() -> None:
    """`update_price` already ran by the time this check executes — proving the
    comparison still uses the floor from before this tick, not a re-read of the
    row `update_price` just rewrote to the new (lower) price."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(user_id=1)
    try:
        pid = await _tracked(repo, floor=Decimal("100"))
        real_update_price = repo.update_price

        async def spying_update_price(product_id: int, price: Decimal) -> None:
            # By the time this returns, `products.lowest_price` is 80 in the DB —
            # a comparison that re-read the row here would see 80, not 100, and
            # `80 < 80` would never fire.
            await real_update_price(product_id, price)

        repo.update_price = spying_update_price  # type: ignore[method-assign]

        notifier = await _check(repo, Decimal("80"))

        assert notifier.await_args is not None
        assert "All-time low" in notifier.await_args.args[1]
        product = await repo.get_product(pid)
        assert product is not None
        assert product.lowest_price == Decimal("80")  # update_price still ran
    finally:
        await conn.close()


# ── The formatter ─────────────────────────────────────────────────────────


def test_the_formatter_names_the_previous_floor() -> None:
    from price_tracker.core.alert import PriceAlert

    alert = PriceAlert(
        product_id=1,
        product_name="Widget",
        url="https://example.com/p/1",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="all_time_low",
        threshold_value=Decimal("0"),
        previous_low=Decimal("90"),
    )

    text = format_all_time_low(alert)

    assert "All-time low" in text
    assert "80" in text
    assert "90" in text


def test_the_formatter_survives_no_previous_floor() -> None:
    """A product with no prior `lowest_price` should not reach this formatter in
    practice (`atl_hit` requires one) — it must not raise if it ever does."""
    from price_tracker.core.alert import PriceAlert

    alert = PriceAlert(
        product_id=1,
        product_name="Widget",
        url="https://example.com/p/1",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="all_time_low",
        threshold_value=Decimal("0"),
    )

    text = format_all_time_low(alert)

    assert "80" in text


@pytest.mark.asyncio
async def test_a_correct_reading_is_never_suppressed_as_a_duplicate(
    repo: Repository,
) -> None:
    """Two consecutive new lows must both alert — cooldown dedup is keyed on
    price staying flat-or-above, and every all-time-low tick is a new floor."""
    pid = await _tracked(repo, floor=Decimal("100"))
    await _check(repo, Decimal("90"))
    notifier = await _check(repo, Decimal("80"))

    assert notifier.await_args is not None
    product = await repo.get_product(pid)
    assert product is not None
    assert product.lowest_price == Decimal("80")


# ── The reader's own language ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_all_time_low_alert_speaks_the_readers_language(repo: Repository) -> None:
    """Both formatters translate through `_()`; choosing between them must
    happen inside the reader's locale, not before it is set."""
    await repo.update_user_info(1, language_code="es")
    await _tracked(repo, floor=Decimal("100"))

    notifier = await _check(repo, Decimal("80"))

    assert notifier.await_args is not None
    assert "más bajo" in notifier.await_args.args[1].lower()
