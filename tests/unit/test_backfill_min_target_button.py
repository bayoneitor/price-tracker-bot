"""`bfmin_target_<id>` — the confirmation card's shortcut to the imported floor.

Offered only right after a backfill found one, and it never asks the reader to
type it back: the number is already `product["lowest_price"]`, folded in by
`add_price_history_bulk` the moment the backfill wrote its points, and this
button reuses the same `set_target_price` `/target`'s typed prompt calls.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._product import handle_track_choice


def _query() -> MagicMock:
    return MagicMock(message=MagicMock(message_id=10), edit_message_text=AsyncMock())


def _db(product: dict[str, Any] | None) -> AsyncMock:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=product)
    db.set_target_price = AsyncMock()
    return db


def _context(db: AsyncMock) -> MagicMock:
    # `resolve_owned_product` reaches the db through `bot_data["db"]`, not
    # through the `db` argument the handler itself is called with.
    context = MagicMock()
    context.bot_data = {"db": db}
    return context


@pytest.mark.asyncio
async def test_sets_the_target_to_the_imported_floor() -> None:
    product = {"id": 5, "name": "Widget", "lowest_price": "35.00", "currency": "EUR"}
    db = _db(product)
    query = _query()

    handled = await handle_track_choice(query, _context(db), db, 1, "bfmin_target_5")

    assert handled is True
    db.set_target_price.assert_awaited_once_with(5, Decimal("35.00"))


@pytest.mark.asyncio
async def test_confirms_with_the_price_it_set() -> None:
    product = {"id": 5, "name": "Widget", "lowest_price": "35.00", "currency": "EUR"}
    db = _db(product)
    query = _query()

    await handle_track_choice(query, _context(db), db, 1, "bfmin_target_5")

    text = query.edit_message_text.await_args.args[0]
    assert "35.00" in text


@pytest.mark.asyncio
async def test_a_product_with_no_imported_price_sets_nothing() -> None:
    """Reachable only from a card the backfill itself produced — a stale button
    pointing at a product that never had one must not silently target 0."""
    product = {"id": 5, "name": "Widget", "lowest_price": None, "currency": "EUR"}
    db = _db(product)
    query = _query()

    handled = await handle_track_choice(query, _context(db), db, 1, "bfmin_target_5")

    assert handled is True
    db.set_target_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_someone_elses_product_is_refused() -> None:
    """Ownership goes through the same `resolve_owned_product` every per-product
    callback uses — ensuring this new button did not skip it."""
    db = _db(None)
    query = _query()

    handled = await handle_track_choice(query, _context(db), db, 1, "bfmin_target_5")

    assert handled is True
    db.set_target_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_callbacks_fall_through() -> None:
    assert not await handle_track_choice(_query(), MagicMock(), _db(None), 1, "check_5")
