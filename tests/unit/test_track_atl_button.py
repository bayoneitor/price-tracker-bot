"""`track_atl_<id>` — the button that opts a product into the `all_time_low` trigger."""

from __future__ import annotations

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
    db.set_threshold = AsyncMock()
    return db


def _context(db: AsyncMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db}
    return context


@pytest.mark.asyncio
async def test_sets_the_threshold_type_to_all_time_low() -> None:
    product = {"id": 5, "name": "Widget"}
    db = _db(product)

    handled = await handle_track_choice(_query(), _context(db), db, 1, "track_atl_5")

    assert handled is True
    db.set_threshold.assert_awaited_once_with(5, "all_time_low", "0")


@pytest.mark.asyncio
async def test_confirms_in_the_reply() -> None:
    product = {"id": 5, "name": "Widget"}
    db = _db(product)
    query = _query()

    await handle_track_choice(query, _context(db), db, 1, "track_atl_5")

    assert "All-time low" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_someone_elses_product_is_refused() -> None:
    db = _db(None)

    handled = await handle_track_choice(_query(), _context(db), db, 1, "track_atl_5")

    assert handled is True
    db.set_threshold.assert_not_awaited()
