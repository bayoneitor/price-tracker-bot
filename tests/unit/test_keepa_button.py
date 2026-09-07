"""`keepa_<id>` — sends Keepa's own free PNG, no plugin involved.

Same shape as the chart button (`_chart.handle_chart_button`): the panel is
deleted and the image sent, and the navigation trail moves with it so ◀️ Back
reopens the panel rather than dead-ending on a deleted message.

The button now also sits under the bot's own chart, so the message it arrives
on may be a photo — and Telegram will not edit text into one.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._keepa import handle_keepa_button
from price_tracker.bot.navigation import NAV_KEY


def _query(*, photo: bool = False) -> MagicMock:
    message = MagicMock(message_id=10)
    message.delete = AsyncMock()
    message.reply_photo = AsyncMock(return_value=MagicMock(message_id=20))
    # A text message from Telegram carries an empty `photo` tuple, not no
    # attribute at all — which is exactly what the handler reads to decide
    # whether it may edit text.
    message.photo = (MagicMock(),) if photo else ()
    return MagicMock(
        message=message, edit_message_text=AsyncMock(), edit_message_caption=AsyncMock()
    )


def _db(product: dict[str, Any] | None) -> AsyncMock:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=product)
    return db


PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def _context(db: AsyncMock, *, response: MagicMock | None = None) -> MagicMock:
    """A context whose shared HTTP client answers the Keepa graph request.

    The handler downloads the PNG itself — Keepa serves that endpoint by
    User-Agent and 403s anything that is not a browser, Telegram's own
    fetcher included — so a test has to provide the client, not just the db.
    """
    if response is None:
        response = MagicMock(status_code=200, content=PNG, headers={"content-type": "image/png"})
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    context = MagicMock()
    context.bot_data = {"db": db, "http_client": client}
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
async def test_asks_keepa_for_the_right_graph() -> None:
    db = _db(_amazon_product())
    query = _query()
    context = _context(db)

    handled = await handle_keepa_button(query, context, db, 1, "keepa_5")

    assert handled is True
    requested = context.bot_data["http_client"].get.await_args.args[0]
    assert requested == (
        "https://graph.keepa.com/pricehistory.png?asin=B0CX23V2ZK&domain=9&range=365"
    )


@pytest.mark.asyncio
async def test_the_request_carries_a_browser_user_agent() -> None:
    """Keepa 403s anything that is not a browser — including Telegram's own
    fetcher, which is why the URL is not simply handed to `reply_photo`."""
    db = _db(_amazon_product())
    context = _context(db)

    await handle_keepa_button(_query(), context, db, 1, "keepa_5")

    headers = context.bot_data["http_client"].get.await_args.kwargs["headers"]
    assert "Mozilla/5.0" in headers["User-Agent"]


@pytest.mark.asyncio
async def test_the_photo_is_uploaded_not_linked() -> None:
    db = _db(_amazon_product())
    query = _query()

    await handle_keepa_button(query, _context(db), db, 1, "keepa_5")

    photo = query.message.reply_photo.await_args.kwargs["photo"]
    assert not isinstance(photo, str)  # bytes we fetched, not a URL


@pytest.mark.asyncio
async def test_a_refused_graph_keeps_the_panel_instead_of_losing_it() -> None:
    """The panel is deleted only once the image is in hand: a 403 used to
    delete it first and then raise on the send, losing both."""
    db = _db(_amazon_product())
    query = _query()
    refused = MagicMock(status_code=403, content=b"<html>no</html>", headers={})

    handled = await handle_keepa_button(query, _context(db, response=refused), db, 1, "keepa_5")

    assert handled is True
    query.message.delete.assert_not_awaited()
    query.message.reply_photo.assert_not_awaited()
    query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_a_non_png_body_with_a_200_is_still_refused() -> None:
    """An HTML error page served with a 200 must not reach Telegram as a photo."""
    db = _db(_amazon_product())
    query = _query()
    html = MagicMock(status_code=200, content=b"<html>nope</html>", headers={})

    await handle_keepa_button(query, _context(db, response=html), db, 1, "keepa_5")

    query.message.reply_photo.assert_not_awaited()


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
async def test_a_refusal_on_a_chart_edits_the_caption_not_the_text() -> None:
    """The button also sits under the bot's own chart. Telegram will not turn a
    photo message back into a text one, so a refusal there has to be said in
    the caption — `edit_message_text` would raise and lose the message."""
    db = _db(_amazon_product())
    query = _query(photo=True)
    refused = MagicMock(status_code=403, content=b"<html>no</html>", headers={})

    await handle_keepa_button(query, _context(db, response=refused), db, 1, "keepa_5")

    query.edit_message_text.assert_not_awaited()
    assert "Keepa" in query.edit_message_caption.await_args.kwargs["caption"]


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
