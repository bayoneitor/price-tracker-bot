"""The paginated `/list` view.

`/list` used to send one message per product, which buried the chat and could
not be dismissed. It is now a single edited message: index on top, one product
card below, paged by buttons or by typing the index number, and closable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from price_tracker.bot.handlers.callbacks._list import handle_list_navigation
from price_tracker.bot.handlers.product_list import (
    LIST_GOTO_PREFIX,
    MAX_JUMP_BUTTONS,
    build_list_view,
    cmd_list,
)
from price_tracker.bot.keyboards import CLOSE_CALLBACK
from price_tracker.bot.navigation import PendingInput


def _product(pid: int, name: str = "Widget") -> dict[str, Any]:
    return {
        "id": pid,
        "name": f"{name} {pid}",
        "url": f"https://www.mediamarkt.es/es/product/_x-{pid}.html",
        "domain": "mediamarkt.es",
        "current_price": "259.00",
        "initial_price": "349.00",
        "currency": "EUR",
        "threshold_type": "percentage",
        "threshold_value": "10",
        "is_active": 1,
    }


def _button_data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_view_is_one_message_with_index_and_current_card() -> None:
    products = [_product(i) for i in range(1, 4)]
    text, markup = build_list_view(products, 1)

    # Index lists every product...
    for position in (1, 2, 3):
        assert f"{position}. Widget {position}" in text
    # ...with the current one marked, and its card rendered below.
    assert "<b>▸ 2. Widget 2</b>" in text
    assert "<b>#2</b>" in text
    assert "🌐 Store: mediamarkt.es" in text
    assert CLOSE_CALLBACK in _button_data(markup)


def test_paging_wraps_at_both_ends() -> None:
    products = [_product(i) for i in range(1, 4)]
    _text, first = build_list_view(products, 0)
    assert f"{LIST_GOTO_PREFIX}2" in _button_data(first)  # ◀ from the first wraps to the last
    _text, last = build_list_view(products, 2)
    assert f"{LIST_GOTO_PREFIX}0" in _button_data(last)  # ▶ from the last wraps to the first


def test_index_is_clamped_not_raised() -> None:
    """A stale button from a listing whose products were deleted must not crash."""
    products = [_product(1), _product(2)]
    text, _markup = build_list_view(products, 99)
    assert "<b>#2</b>" in text
    text, _markup = build_list_view(products, -5)
    assert "<b>#1</b>" in text


def test_empty_listing_still_offers_close() -> None:
    text, markup = build_list_view([], 0)
    assert "no tracked products" in text
    assert _button_data(markup) == [CLOSE_CALLBACK]


def test_single_product_has_no_pager() -> None:
    _text, markup = build_list_view([_product(1)], 0)
    assert not any(d.startswith(LIST_GOTO_PREFIX) for d in _button_data(markup))


def test_jump_buttons_window_on_long_lists() -> None:
    """All numbers while they fit; a window centred on the current one after that."""
    products = [_product(i) for i in range(1, 31)]
    _text, markup = build_list_view(products, 20)
    jumps = [d for d in _button_data(markup) if d.startswith(LIST_GOTO_PREFIX)]
    targets = {int(d.removeprefix(LIST_GOTO_PREFIX)) for d in jumps}
    assert 20 in targets
    # The pager adds prev/current/next, so allow those three beyond the window.
    assert len(targets) <= MAX_JUMP_BUTTONS + 3


def test_long_list_elides_the_index_but_keeps_the_card() -> None:
    """The index must not crowd out the product card in Telegram's 4096 chars."""
    products = [_product(i, "A rather long product name to eat the budget") for i in range(1, 61)]
    text, _markup = build_list_view(products, 0)
    assert "and 40 more" in text
    assert "<b>#1</b>" in text
    assert len(text) < 4096


@pytest.mark.asyncio
async def test_cmd_list_sends_exactly_one_message() -> None:
    """The whole point: no more one-message-per-product."""
    update = MagicMock()
    update.effective_user.id = 7
    update.message.reply_text = AsyncMock(return_value=MagicMock(message_id=555))
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "db": AsyncMock(
            get_active_products=AsyncMock(return_value=[_product(i) for i in range(1, 13)])
        )
    }
    context.bot_data["db"].is_user_allowed = AsyncMock(return_value=True)
    context.bot_data["db"].update_user_info = AsyncMock()

    await cmd_list(update, context)

    assert update.message.reply_text.await_count == 1
    # The message id is remembered so a typed number can steer the listing.
    assert context.user_data["list_message_id"] == 555


@pytest.mark.asyncio
async def test_navigation_edits_in_place_and_rereads_products() -> None:
    query = MagicMock(edit_message_text=AsyncMock())
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1), _product(2)]))
    context = MagicMock(user_data={})

    handled = await handle_list_navigation(query, context, db, 7, f"{LIST_GOTO_PREFIX}1")

    assert handled is True
    db.get_active_products.assert_awaited_once_with(7)
    assert "<b>#2</b>" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_navigation_tolerates_unmodified_rerender() -> None:
    """Tapping the current page number re-renders identical content."""
    query = MagicMock(
        edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified"))
    )
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1)]))

    handled = await handle_list_navigation(
        query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}0"
    )
    assert handled is True


@pytest.mark.asyncio
async def test_navigation_rejects_tampered_index() -> None:
    query = MagicMock(edit_message_text=AsyncMock())
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1), _product(2)]))

    handled = await handle_list_navigation(
        query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}not-a-number"
    )
    assert handled is True
    assert "<b>#1</b>" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_close_deletes_the_message_and_forgets_the_listing() -> None:
    query = MagicMock(message=MagicMock(message_id=555, delete=AsyncMock()))
    context = MagicMock(user_data={"list_message_id": 555})

    handled = await handle_list_navigation(query, context, AsyncMock(), 7, CLOSE_CALLBACK)

    assert handled is True
    query.message.delete.assert_awaited_once()
    assert "list_message_id" not in context.user_data


@pytest.mark.asyncio
async def test_closing_another_panel_keeps_the_listing_reference() -> None:
    """The same button closes the edit panel; that must not orphan an open listing.

    Otherwise dismissing the edit panel would stop a typed index number from
    steering the listing still on screen above it.
    """
    query = MagicMock(message=MagicMock(message_id=999, delete=AsyncMock()))
    context = MagicMock(user_data={"list_message_id": 555})

    handled = await handle_list_navigation(query, context, AsyncMock(), 7, CLOSE_CALLBACK)

    assert handled is True
    query.message.delete.assert_awaited_once()
    assert context.user_data["list_message_id"] == 555


@pytest.mark.asyncio
async def test_close_collapses_when_the_message_is_too_old_to_delete() -> None:
    """Telegram only lets a bot delete its own messages for 48 hours."""
    query = MagicMock(
        message=MagicMock(delete=AsyncMock(side_effect=BadRequest("message can't be deleted"))),
        edit_message_text=AsyncMock(),
    )

    handled = await handle_list_navigation(
        query, MagicMock(user_data={}), AsyncMock(), 7, CLOSE_CALLBACK
    )

    assert handled is True
    assert "closed" in query.edit_message_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_unrelated_callback_falls_through() -> None:
    handled = await handle_list_navigation(
        MagicMock(), MagicMock(user_data={}), AsyncMock(), 7, "check_3"
    )
    assert handled is False


# ── Jumping by typing the index number ───────────────────────────────────


def _text_update(text: str) -> Any:
    update = MagicMock()
    update.effective_user.id = 7
    update.effective_chat.id = 7
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _text_context(products: list[dict[str, Any]], **user_data: Any) -> Any:
    db = AsyncMock(get_active_products=AsyncMock(return_value=products))
    db.is_user_allowed = AsyncMock(return_value=True)
    db.update_user_info = AsyncMock()
    context = MagicMock()
    context.user_data = dict(user_data)
    context.bot_data = {"db": db}
    context.bot.edit_message_text = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_typing_a_number_steers_the_open_listing() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input

    products = [_product(i) for i in range(1, 6)]
    context = _text_context(products, list_message_id=555)

    await handle_text_input(_text_update("3"), context)

    context.bot.edit_message_text.assert_awaited_once()
    kwargs = context.bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 555
    # Users type the 1-based number they see in the index.
    assert "<b>#3</b>" in kwargs["text"]


@pytest.mark.asyncio
async def test_typing_an_out_of_range_number_says_so() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input

    update = _text_update("9")
    context = _text_context([_product(1), _product(2)], list_message_id=555)

    await handle_text_input(update, context)

    context.bot.edit_message_text.assert_not_awaited()
    assert "No product 9" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_a_number_answering_a_prompt_still_goes_to_the_prompt() -> None:
    """A pending picker wins: typing 30 for /refresh must not jump the listing."""
    from price_tracker.bot.handlers.text_input import handle_text_input

    context = _text_context([_product(1)], list_message_id=555)
    context.user_data["pending_action"] = PendingInput("refresh", 1)
    context.bot_data["db"].get_product = AsyncMock(return_value=_product(1))
    context.bot_data["db"].get_product_for_user = AsyncMock(return_value=_product(1))
    context.bot_data["db"].is_user_admin = AsyncMock(return_value=False)
    context.bot_data["db"].set_product_interval = AsyncMock()

    await handle_text_input(_text_update("30"), context)

    context.bot.edit_message_text.assert_not_awaited()
    context.bot_data["db"].set_product_interval.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_number_without_an_open_listing_is_ignored() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input

    update = _text_update("3")
    context = _text_context([_product(1), _product(2), _product(3)])

    await handle_text_input(update, context)

    context.bot.edit_message_text.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_listing_reference_is_dropped() -> None:
    """If the listing was deleted, forget it instead of swallowing later numbers."""
    from price_tracker.bot.handlers.text_input import handle_text_input

    context = _text_context([_product(1), _product(2)], list_message_id=555)
    context.bot.edit_message_text = AsyncMock(side_effect=BadRequest("message to edit not found"))

    await handle_text_input(_text_update("2"), context)

    assert "list_message_id" not in context.user_data


# ── The edit panel must be dismissable too ───────────────────────────────


@pytest.mark.asyncio
async def test_edit_panel_offers_a_close_button() -> None:
    """It opens as a new message, so without this it could only be scrolled past."""
    from price_tracker.bot.handlers.callbacks._actions import handle_edit_button

    query = MagicMock(message=MagicMock(reply_text=AsyncMock()), edit_message_text=AsyncMock())
    db = AsyncMock()
    context = MagicMock()
    context.bot_data = {"db": db}
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=_product(3))

    handled = await handle_edit_button(query, context, db, 7, "edit_3")

    assert handled is True
    markup = query.message.reply_text.await_args.kwargs["reply_markup"]
    assert CLOSE_CALLBACK in _button_data(markup)
