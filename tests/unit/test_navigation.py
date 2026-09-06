"""Every screen with an action must offer a way out, and back must mean something.

Only the delete confirmation had a cancel and only the menus had a back button:
the product pickers, the typed prompts and every result screen were dead ends,
and a chart could only be scrolled past. The trail recorded per message is what
lets a screen reachable from three places offer one working ◀️ Back.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from price_tracker.bot.handlers.callbacks import _nav
from price_tracker.bot.keyboards import (
    BACK_CALLBACK,
    CANCEL_CALLBACK,
    CLOSE_CALLBACK,
    nav_row,
    prompt_keyboard,
    result_keyboard,
)
from price_tracker.bot.navigation import (
    NAV_DEPTH,
    PendingInput,
    pop_nav,
    previous_nav,
    push_nav,
    transfer_nav,
)

MESSAGE_ID = 100


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


def _data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ── the trail ────────────────────────────────────────────────────────────


def test_back_goes_to_the_screen_before_this_one() -> None:
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")
    push_nav(context, MESSAGE_ID, "menu_prodotti")
    push_nav(context, MESSAGE_ID, "edit_3")

    # Only the screen being left comes off; the target stays on top because the
    # caller renders it directly rather than through the dispatcher.
    assert pop_nav(context, MESSAGE_ID) == "menu_prodotti"
    assert pop_nav(context, MESSAGE_ID) == "menu_main"


def test_the_first_screen_has_nowhere_to_go_back_to() -> None:
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")

    # The top of the trail is the screen currently on display, not a destination.
    assert previous_nav(context, MESSAGE_ID) is None
    assert pop_nav(context, MESSAGE_ID) is None


def test_paging_a_listing_collapses_onto_one_entry() -> None:
    """Otherwise back would walk the user through every page turn before leaving."""
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")
    for page in range(6):
        push_nav(context, MESSAGE_ID, f"list_go_{page}")

    assert context.user_data["nav"][MESSAGE_ID] == ["menu_main", "list_go_5"]


def test_a_trail_cannot_grow_without_bound() -> None:
    context = _context()
    for step in range(NAV_DEPTH + 5):
        push_nav(context, MESSAGE_ID, f"screen_{step}")

    assert len(context.user_data["nav"][MESSAGE_ID]) == NAV_DEPTH


def test_a_trail_can_move_to_another_message() -> None:
    """Opening a chart replaces the panel, so the trail has to follow the photo."""
    context = _context()
    push_nav(context, MESSAGE_ID, "list_go_2")
    push_nav(context, MESSAGE_ID, "chart_3")

    transfer_nav(context, MESSAGE_ID, 999)

    assert context.user_data["nav"][999] == ["list_go_2", "chart_3"]
    assert context.user_data["nav"].get(MESSAGE_ID) is None
    # …so back from the photo lands on the listing it replaced.
    assert pop_nav(context, 999) == "list_go_2"


# ── the buttons ──────────────────────────────────────────────────────────


def test_no_back_button_when_there_is_no_trail() -> None:
    context = _context()

    assert BACK_CALLBACK not in [b.callback_data for b in nav_row(context, MESSAGE_ID)]


def test_no_back_button_on_the_first_screen_of_a_trail() -> None:
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")

    assert BACK_CALLBACK not in [b.callback_data for b in nav_row(context, MESSAGE_ID)]


def test_back_button_appears_once_there_is_somewhere_to_go() -> None:
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")
    push_nav(context, MESSAGE_ID, "menu_prodotti")

    assert BACK_CALLBACK in [b.callback_data for b in nav_row(context, MESSAGE_ID)]


def test_re_rendering_the_same_screen_does_not_grow_a_back_button() -> None:
    """Paging a listing must not change the shape of its button row.

    The trail used to be recorded after the screen drew itself, so paging left the
    listing on top of its own trail: from the second page on it showed a ◀️ Back
    that led to the main menu, and every button in that row shifted one place.
    """
    context = _context()
    push_nav(context, MESSAGE_ID, "list_go_0")
    first = [b.callback_data for b in nav_row(context, MESSAGE_ID)]

    for page in range(1, 4):
        push_nav(context, MESSAGE_ID, f"list_go_{page}")
        assert [b.callback_data for b in nav_row(context, MESSAGE_ID)] == first


def test_a_prompt_always_offers_at_least_a_cancel() -> None:
    assert CANCEL_CALLBACK in _data(prompt_keyboard(_context(), MESSAGE_ID))


def test_a_result_screen_always_offers_at_least_a_close() -> None:
    assert CLOSE_CALLBACK in _data(result_keyboard(_context(), MESSAGE_ID))


# ── the three exits ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_drops_the_answer_the_bot_was_waiting_for() -> None:
    context = _context()
    context.user_data["pending_action"] = PendingInput("target", 3)
    query = MagicMock(edit_message_text=AsyncMock())

    assert await _nav.handle_cancel(query, context, CANCEL_CALLBACK) is True
    assert "pending_action" not in context.user_data


@pytest.mark.asyncio
async def test_close_forgets_the_trail_it_leaves_behind() -> None:
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")
    query = MagicMock(message=MagicMock(message_id=MESSAGE_ID, delete=AsyncMock()))

    assert await _nav.handle_close(query, context, CLOSE_CALLBACK) is True
    assert context.user_data["nav"] == {}


@pytest.mark.asyncio
async def test_back_re_dispatches_the_previous_screen() -> None:
    context = _context()
    push_nav(context, MESSAGE_ID, "menu_main")
    push_nav(context, MESSAGE_ID, "menu_prodotti")
    query = MagicMock(message=MagicMock(message_id=MESSAGE_ID, text="a panel"))
    dispatch = AsyncMock(return_value=True)

    handled = await _nav.handle_back(query, context, AsyncMock(), 7, BACK_CALLBACK, dispatch)

    assert handled is True
    assert dispatch.call_args.args[4] == "menu_main"
    # …and the screen we landed on is on top, with nothing further back.
    assert context.user_data["nav"][MESSAGE_ID] == ["menu_main"]
    assert previous_nav(context, MESSAGE_ID) is None


@pytest.mark.asyncio
async def test_back_with_no_trail_falls_back_to_the_main_menu() -> None:
    """The trail lives in memory, so a restart leaves old panels without one."""
    query = MagicMock(message=MagicMock(message_id=MESSAGE_ID, text="a panel"))
    dispatch = AsyncMock(return_value=True)

    await _nav.handle_back(query, _context(), AsyncMock(), 7, BACK_CALLBACK, dispatch)

    assert dispatch.call_args.args[4] == _nav.FALLBACK_SCREEN


@pytest.mark.asyncio
async def test_back_from_a_chart_replaces_the_photo_with_the_panel() -> None:
    """Telegram will not turn a photo back into text, so the panel is re-sent.

    The photo is then removed: leaving every chart behind filled the chat with
    old images nobody was looking at any more.
    """
    context = _context()
    push_nav(context, MESSAGE_ID, "list_go_2")
    push_nav(context, MESSAGE_ID, "chart_3")
    sent = MagicMock(message_id=555)
    query = MagicMock(
        message=MagicMock(
            message_id=MESSAGE_ID,
            text=None,
            reply_text=AsyncMock(return_value=sent),
            delete=AsyncMock(),
        ),
        edit_message_reply_markup=AsyncMock(),
    )

    async def dispatch(renderer: Any, *args: Any) -> bool:
        await renderer.edit_message_text("the listing")
        return True

    handled = await _nav.handle_back(query, context, AsyncMock(), 7, BACK_CALLBACK, dispatch)

    assert handled is True
    query.message.reply_text.assert_awaited_once()
    query.message.delete.assert_awaited_once()
    # The trail moved to the panel that is now on screen.
    assert context.user_data["nav"][555] == ["list_go_2"]
    assert context.user_data["nav"].get(MESSAGE_ID) is None


@pytest.mark.asyncio
async def test_a_chart_too_old_to_delete_is_at_least_disarmed() -> None:
    """A bot may only delete its own messages for 48 hours."""
    context = _context()
    push_nav(context, MESSAGE_ID, "list_go_2")
    push_nav(context, MESSAGE_ID, "chart_3")
    query = MagicMock(
        message=MagicMock(
            message_id=MESSAGE_ID,
            text=None,
            reply_text=AsyncMock(return_value=MagicMock(message_id=555)),
            delete=AsyncMock(side_effect=BadRequest("message can't be deleted")),
        ),
        edit_message_reply_markup=AsyncMock(),
    )

    async def dispatch(renderer: Any, *args: Any) -> bool:
        await renderer.edit_message_text("the listing")
        return True

    await _nav.handle_back(query, context, AsyncMock(), 7, BACK_CALLBACK, dispatch)

    # It stays, but cannot open a second panel.
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


# ── The locale a handler sets must not outlive it ────────────────────────


@pytest.mark.asyncio
async def test_with_locale_restores_the_language_it_found() -> None:
    """Each update runs in its own context copy, so nothing has leaked yet — but a
    handler that ever shares one would inherit whichever language ran last."""
    from price_tracker.bot.decorators import with_locale
    from price_tracker.bot.messages import _ as translate
    from price_tracker.bot.messages import set_locale

    set_locale("en")
    before = translate("✖ Close")

    @with_locale
    async def handler(update: Any, context: Any) -> str:
        return translate("✖ Close")

    update = MagicMock()
    update.effective_user.language_code = "es"

    inside = await handler(update, MagicMock())

    assert inside != before, "the handler did not get its own language"
    assert translate("✖ Close") == before, "the language outlived the handler"
