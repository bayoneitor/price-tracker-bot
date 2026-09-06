"""The main menu must look the same however you arrive at it.

`/menu` sent one menu and the ◀️ Menu button rendered another: two columns
against one, a different label on all but one entry, and a different heading.
Going back to the menu rearranged it under the reader, which reads as the bot
losing its place.

They are one builder now, and this pins them together.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers import _send_main_menu
from price_tracker.bot.handlers.callbacks._menu import handle_menu_navigation
from price_tracker.bot.keyboards import build_main_menu


def _layout(markup: Any) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _targets(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _sent(is_admin: bool) -> tuple[str, Any]:
    message = MagicMock(reply_text=AsyncMock())
    await _send_main_menu(message, is_admin=is_admin)
    call = message.reply_text.await_args
    return call.args[0], call.kwargs["reply_markup"]


async def _via_back_button(is_admin: bool) -> tuple[str, Any]:
    db = AsyncMock(is_user_admin=AsyncMock(return_value=is_admin))
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())
    query.from_user.id = 1
    context = MagicMock()
    context.user_data = {}
    await handle_menu_navigation(query, context, db, 1, "menu_main")
    call = query.edit_message_text.await_args
    return call.args[0], call.kwargs["reply_markup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [False, True])
async def test_arriving_by_command_and_by_button_gives_the_same_menu(is_admin: bool) -> None:
    sent_text, sent_markup = await _sent(is_admin)
    back_text, back_markup = await _via_back_button(is_admin)

    assert sent_text == back_text
    assert _layout(sent_markup) == _layout(back_markup)
    assert _targets(sent_markup) == _targets(back_markup)


def test_the_admin_entry_is_the_only_thing_that_varies() -> None:
    _plain_text, plain = build_main_menu(is_admin=False)
    _admin_text, admin = build_main_menu(is_admin=True)

    assert _layout(admin)[: len(_layout(plain))] == _layout(plain)
    assert _targets(admin) == [*_targets(plain), "menu_admin"]


def test_every_entry_leads_somewhere_the_dispatcher_knows() -> None:
    """A menu button whose callback nothing claims is a button that does nothing."""
    _text, markup = build_main_menu(is_admin=True)

    assert _targets(markup) == [
        "menu_prodotti",
        "menu_prezzi",
        "menu_notifiche",
        "menu_dati",
        "menu_info",
        "menu_admin",
    ]
