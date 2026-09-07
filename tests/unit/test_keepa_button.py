"""`keepa_<id>` — sends Keepa's own free PNG, no plugin involved.

Same shape as the chart button (`_product.handle_chart_button`): the panel is
deleted and the image sent, and the navigation trail moves with it so ◀️ Back
reopens the panel rather than dead-ending on a deleted message.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._keepa import handle_keepa_button
from price_tracker.bot.navigation import NAV_KEY


def _query() -> MagicMock:
    message = MagicMock(message_id=10)
    message.delete = AsyncMock()
    message.reply_photo = AsyncMock(return_value=MagicMock(message_id=20))
    return MagicMock(message=message, edit_message_text=AsyncMock())


def _db(product: dict[str, Any] | None) -> AsyncMock:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=product)
    return db


def _context(db: AsyncMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db}
    context.user_data = {NAV_KEY: {10: ["prod_5"]}}
    return context


def _amazon_product(**extra: Any) -> dict[str, Any]:
    return {
        "id": 5,
        "name": "Monitor",
        "url": "https://www.amazon.es/dp/B0CX23V2ZK",
        "domain": "amazon.es",
        **extra,
    }


@pytest.mark.asyncio
async def test_sends_keepas_own_png_by_url() -> None:
    db = _db(_amazon_product())
    query = _query()

    handled = await handle_keepa_button(query, _context(db), db, 1, "keepa_5")

    assert handled is True
    kwargs = query.message.reply_photo.await_args.kwargs
    assert (
        kwargs["photo"]
        == "https://graph.keepa.com/pricehistory.png?asin=B0CX23V2ZK&domain=9&range=365"
    )


@pytest.mark.asyncio
async def test_the_caption_credits_keepa() -> None:
    db = _db(_amazon_product())
    query = _query()

    await handle_keepa_button(query, _context(db), db, 1, "keepa_5")

    assert "Keepa" in query.message.reply_photo.await_args.kwargs["caption"]


@pytest.mark.asyncio
async def test_the_panel_is_deleted_and_the_trail_follows_the_photo() -> None:
    db = _db(_amazon_product())
    query = _query()
    context = _context(db)

    await handle_keepa_button(query, context, db, 1, "keepa_5")

    query.message.delete.assert_awaited_once()
    assert context.user_data[NAV_KEY][20] == ["prod_5"]
    assert 10 not in context.user_data[NAV_KEY]


@pytest.mark.asyncio
async def test_a_product_with_no_asin_says_so_rather_than_sending_nothing() -> None:
    non_amazon = _amazon_product(url="https://www.pccomponentes.com/monitor")
    db = _db(non_amazon)
    query = _query()

    handled = await handle_keepa_button(query, _context(db), db, 1, "keepa_5")

    assert handled is True
    query.message.reply_photo.assert_not_awaited()
    assert "ASIN" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_someone_elses_product_is_refused() -> None:
    db = _db(None)
    query = _query()

    handled = await handle_keepa_button(query, _context(db), db, 1, "keepa_5")

    assert handled is True
    query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_callbacks_fall_through() -> None:
    db = _db(None)

    assert not await handle_keepa_button(_query(), _context(db), db, 1, "chart_5")
