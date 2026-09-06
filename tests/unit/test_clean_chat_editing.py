"""Answering a prompt must not leave three messages behind.

A single "set a target price" exchange used to cost the chat four messages: the
question, the typed answer, the bot's confirmation, and another pair for every
rejected value. The answer is now shown by editing the question, and the typed
value is deleted, so the exchange stays one message wherever the user is looking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from price_tracker.bot.handlers.text_input import cmd_cancel, handle_text_input
from price_tracker.bot.navigation import PendingInput

PROMPT_ID = 77
CHAT_ID = 5


def _update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.delete = AsyncMock()
    return update


def _context(pending: PendingInput) -> MagicMock:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.update_user_info = AsyncMock()
    db.get_product_for_user = AsyncMock(
        return_value={"name": "Widget", "current_price": "100", "currency": "EUR"}
    )
    context = MagicMock()
    context.user_data = {"pending_action": pending}
    context.bot_data = {"db": db}
    context.bot.edit_message_text = AsyncMock()
    return context


def _pending() -> PendingInput:
    return PendingInput("target", 3, prompt_message_id=PROMPT_ID, chat_id=CHAT_ID)


@pytest.mark.asyncio
async def test_the_answer_is_shown_in_the_question_and_the_typing_removed() -> None:
    context = _context(_pending())
    update = _update("29.99")

    await handle_text_input(update, context)

    kwargs = context.bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == PROMPT_ID
    assert kwargs["chat_id"] == CHAT_ID
    assert "29.99" in kwargs["text"]
    update.message.delete.assert_awaited_once()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_rejected_value_rewrites_the_question_instead_of_stacking() -> None:
    context = _context(_pending())
    update = _update("not a price")

    await handle_text_input(update, context)

    assert "Invalid price" in context.bot.edit_message_text.await_args.kwargs["text"]
    update.message.reply_text.assert_not_awaited()
    # …and the prompt is still armed, so the next message is another attempt.
    assert context.user_data["pending_action"] == _pending()


@pytest.mark.asyncio
async def test_the_answer_survives_when_the_question_is_gone() -> None:
    """Too old to edit, or deleted by the user: reply rather than swallow it."""
    context = _context(_pending())
    context.bot.edit_message_text = AsyncMock(side_effect=BadRequest("message to edit not found"))
    update = _update("29.99")

    await handle_text_input(update, context)

    update.message.reply_text.assert_awaited_once()
    # Nothing was shown in place, so the typed value stays as the only record.
    update.message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_prompt_that_never_recorded_its_message_still_answers() -> None:
    context = _context(PendingInput("target", 3))
    update = _update("29.99")

    await handle_text_input(update, context)

    context.bot.edit_message_text.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_closes_the_prompt_it_was_typed_under() -> None:
    context = _context(_pending())
    update = _update("/cancel")

    await cmd_cancel(update, context)

    assert "pending_action" not in context.user_data
    assert "Cancelled" in context.bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_cancel_with_nothing_open_says_so() -> None:
    context = _context(_pending())
    del context.user_data["pending_action"]
    update = _update("/cancel")

    await cmd_cancel(update, context)

    assert "Nothing to cancel" in update.message.reply_text.await_args.args[0]
