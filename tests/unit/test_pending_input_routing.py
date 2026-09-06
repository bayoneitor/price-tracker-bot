"""A pasted link must not hijack a prompt that is already waiting for an answer.

`handle_url` was registered before `handle_text_input`, both in python-telegram-bot's
default handler group, where only the first match runs. So any message containing a
URL went to "track this product" no matter what the bot had just asked. The visible
casualty was `/menu → Admin → Scraper debug`: it asks for a product URL and the
paste was tracked as a new product instead of analysed.

There is now one text handler that checks the open prompt first.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.handlers.text_input import handle_text_input
from price_tracker.bot.navigation import PendingInput

URL = "https://www.mediamarkt.es/es/product/_x-1.html"


def _update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.effective_chat.id = 1
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _context(**user_data: Any) -> MagicMock:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=True)
    db.update_user_info = AsyncMock()
    db.get_active_products = AsyncMock(return_value=[])
    context = MagicMock()
    context.user_data = dict(user_data)
    context.bot_data = {"db": db}
    return context


@pytest.mark.asyncio
async def test_a_url_answers_the_debug_prompt_instead_of_being_tracked() -> None:
    context = _context(pending_action=PendingInput("admin_debug"))
    update = _update(URL)

    with (
        patch("price_tracker.bot.handlers.debug.cmd_debug", new=AsyncMock()) as debug,
        patch("price_tracker.bot.handlers.product._add_product", new=AsyncMock()) as add,
    ):
        await handle_text_input(update, context)

    debug.assert_awaited_once()
    assert context.args == [URL]
    add.assert_not_awaited()
    assert "pending_action" not in context.user_data


@pytest.mark.asyncio
async def test_a_url_with_no_prompt_open_still_tracks_the_product() -> None:
    context = _context()
    update = _update(f"look at this {URL} nice price")

    with patch("price_tracker.bot.handlers.product._add_product", new=AsyncMock()) as add:
        await handle_text_input(update, context)

    add.assert_awaited_once()
    assert add.call_args.args[2] == URL


@pytest.mark.asyncio
async def test_a_url_sent_to_a_prompt_that_wants_a_price_is_refused_not_tracked() -> None:
    """Neither outcome may be silent: the prompt stays open and says what it wants."""
    context = _context(pending_action=PendingInput("target", 7))
    update = _update(URL)

    with patch("price_tracker.bot.handlers.product._add_product", new=AsyncMock()) as add:
        await handle_text_input(update, context)

    add.assert_not_awaited()
    assert context.user_data["pending_action"] == PendingInput("target", 7)
    reply = update.message.reply_text.await_args.args[0]
    assert "target price" in reply
    assert "/cancel" in reply


@pytest.mark.asyncio
async def test_an_unusable_answer_keeps_the_prompt_open() -> None:
    context = _context(pending_action=PendingInput("target", 7))
    context.bot_data["db"].get_product = AsyncMock(return_value={"name": "Widget"})
    update = _update("not a price")

    await handle_text_input(update, context)

    assert context.user_data["pending_action"] == PendingInput("target", 7)
    assert "Invalid price" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_a_cancel_word_closes_the_prompt() -> None:
    context = _context(pending_action=PendingInput("target", 7))

    await handle_text_input(_update("cancelar"), context)

    assert "pending_action" not in context.user_data
