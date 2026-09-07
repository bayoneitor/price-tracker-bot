"""`add_price_history_bulk` — the write path a backfill provider's points land on.

`add_price_history` cannot set `checked_at`: every row it writes gets the moment
it runs, right for a live check and wrong for a point a provider says happened
months ago. The bulk write sets each point's own timestamp, tags every row with
the provider's name so it is never mistaken for a live read (`source IS NULL`),
and folds the batch's own low and high into the product's own `lowest_price` /
`highest_price` — the same row a live check updates, in one transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest_asyncio

from price_tracker.core.history_base import HistoryPoint
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


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


async def _product(repo: Repository) -> int:
    return await repo.add_product(
        user_id=1,
        url="https://amazon.es/dp/B01",
        name="Widget",
        domain="amazon.es",
        initial_price=Decimal("50"),
        currency="EUR",
    )


def _points(*prices: str) -> tuple[HistoryPoint, ...]:
    return tuple(
        HistoryPoint(observed_at=datetime(2025, 1, i + 1, tzinfo=UTC), price=Decimal(p))
        for i, p in enumerate(prices)
    )


async def test_the_points_land_with_their_own_timestamps(repo: Repository) -> None:
    pid = await _product(repo)

    written = await repo.add_price_history_bulk(pid, _points("40", "45"), source="keepa")

    assert written == 2
    history = await repo.get_price_history(pid, limit=10)
    assert {h.checked_at[:10] for h in history} == {"2025-01-01", "2025-01-02"}


async def test_every_imported_row_carries_the_providers_name(repo: Repository) -> None:
    pid = await _product(repo)

    await repo.add_price_history_bulk(pid, _points("40"), source="keepa")

    history = await repo.get_price_history(pid, limit=10)
    assert history[0].source == "keepa"


async def test_a_live_check_is_still_unsourced(repo: Repository) -> None:
    """NULL means the bot's own read — unchanged for every row written before this."""
    pid = await _product(repo)

    await repo.add_price_history(pid, Decimal("49"))

    history = await repo.get_price_history(pid, limit=10)
    assert history[0].source is None


async def test_the_batchs_floor_becomes_the_products_lowest_price(repo: Repository) -> None:
    """`add_product` seeds both extremes from `initial_price` (50 here), so only
    a genuinely lower import moves the floor — 45 stays below the seeded high."""
    pid = await _product(repo)

    await repo.add_price_history_bulk(pid, _points("40", "35", "45"), source="keepa")

    product = await repo.get_product(pid)
    assert product is not None
    assert product.lowest_price == Decimal("35")
    assert product.highest_price == Decimal("50")


async def test_an_existing_lower_price_is_not_overwritten_by_a_higher_import(
    repo: Repository,
) -> None:
    """A live read already lower than anything imported must survive the backfill."""
    pid = await _product(repo)
    await repo.update_price(pid, Decimal("30"))

    await repo.add_price_history_bulk(pid, _points("40", "45"), source="keepa")

    product = await repo.get_product(pid)
    assert product is not None
    assert product.lowest_price == Decimal("30")


async def test_a_genuinely_lower_import_does_beat_the_existing_floor(repo: Repository) -> None:
    pid = await _product(repo)
    await repo.update_price(pid, Decimal("30"))

    await repo.add_price_history_bulk(pid, _points("40", "20"), source="keepa")

    product = await repo.get_product(pid)
    assert product is not None
    assert product.lowest_price == Decimal("20")


async def test_the_product_records_who_backfilled_it_and_that_it_happened(
    repo: Repository,
) -> None:
    pid = await _product(repo)

    await repo.add_price_history_bulk(pid, _points("40"), source="keepa")

    product = await repo.get_product(pid)
    assert product is not None
    assert product.history_source == "keepa"
    assert product.history_backfilled_at is not None


async def test_an_empty_batch_writes_nothing_and_touches_no_row(repo: Repository) -> None:
    pid = await _product(repo)

    written = await repo.add_price_history_bulk(pid, (), source="keepa")

    assert written == 0
    product = await repo.get_product(pid)
    assert product is not None
    assert product.history_source is None
    assert await repo.get_price_history(pid, limit=10) == []


async def test_a_product_with_no_backfill_has_no_provenance(repo: Repository) -> None:
    pid = await _product(repo)

    product = await repo.get_product(pid)

    assert product is not None
    assert product.history_source is None
    assert product.history_backfilled_at is None
    assert product.created_at is not None
