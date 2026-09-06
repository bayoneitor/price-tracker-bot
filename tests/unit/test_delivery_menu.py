"""Delivery settings behind buttons, not just typed commands.

Muting, quiet hours, timezone, rate limit and digest mode existed only as
commands with a usage string — knowing they existed and remembering the syntax
was the price of admission for the settings people reach for precisely when a
notification has just woken them up.

The menu writes through the same `update_prefs` read-before-write path as the
commands, so neither can clobber fields the other owns.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._delivery import handle_delivery_menu
from price_tracker.db.models import NotificationPrefs

USER_ID = 42


def _context(existing: NotificationPrefs | None = None) -> MagicMock:
    repo = AsyncMock()
    repo.get_notification_prefs = AsyncMock(return_value=existing)
    repo.upsert_notification_prefs = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"repository": repo, "digest_service": AsyncMock()}
    return context


def _query() -> MagicMock:
    return MagicMock(message=MagicMock(message_id=9), edit_message_text=AsyncMock())


def _written(context: MagicMock) -> Any:
    return context.bot_data["repository"].upsert_notification_prefs.call_args.args[0]


def _data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


@pytest.mark.asyncio
async def test_the_panel_shows_effective_settings_not_an_empty_row() -> None:
    """A user with no row of their own still has settings: the defaults."""
    context = _context()
    query = _query()

    assert await handle_delivery_menu(query, context, AsyncMock(), USER_ID, "menu_delivery")

    text = query.edit_message_text.await_args.args[0]
    assert "Europe/Rome" in text
    assert "dlv_mute" in _data(query.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_muting_for_a_fixed_window_records_when_it_ends() -> None:
    context = _context()

    assert await handle_delivery_menu(_query(), context, AsyncMock(), USER_ID, "dlv_mute_8")

    prefs = _written(context)
    assert prefs.mute is True
    assert prefs.mute_until is not None


@pytest.mark.asyncio
async def test_muting_forever_leaves_no_expiry() -> None:
    context = _context()

    await handle_delivery_menu(_query(), context, AsyncMock(), USER_ID, "dlv_mute_forever")

    prefs = _written(context)
    assert prefs.mute is True
    assert prefs.mute_until is None


@pytest.mark.asyncio
async def test_toggling_delivery_never_clobbers_the_other_settings() -> None:
    """upsert writes the whole row, so the menu must read before it writes."""
    existing = NotificationPrefs(
        user_id=USER_ID,
        product_id=None,
        timezone="Europe/Berlin",
        throttle_per_hour=5,
        quiet_hours_start="22:00",
        quiet_hours_end="08:00",
    )
    context = _context(existing)

    await handle_delivery_menu(_query(), context, AsyncMock(), USER_ID, "dlv_digest")

    prefs = _written(context)
    assert prefs.digest_mode is True
    assert prefs.timezone == "Europe/Berlin"
    assert prefs.throttle_per_hour == 5
    assert prefs.quiet_hours_start == "22:00"


@pytest.mark.asyncio
async def test_unmuting_clears_the_expiry_too() -> None:
    context = _context(NotificationPrefs(user_id=USER_ID, product_id=None, mute=True))

    await handle_delivery_menu(_query(), context, AsyncMock(), USER_ID, "dlv_unmute")

    prefs = _written(context)
    assert prefs.mute is False
    assert prefs.mute_until is None


@pytest.mark.asyncio
async def test_the_typed_settings_arm_a_prompt_with_a_way_out() -> None:
    context = _context()
    query = _query()

    assert await handle_delivery_menu(query, context, AsyncMock(), USER_ID, "dlv_timezone")

    assert context.user_data["pending_action"].action == "timezone"
    assert "cancel_action" in _data(query.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_send_digest_now_flushes_and_reports() -> None:
    context = _context()
    context.bot_data["digest_service"].flush_user = AsyncMock(return_value=3)
    query = _query()

    await handle_delivery_menu(query, context, AsyncMock(), USER_ID, "dlv_flush")

    context.bot_data["digest_service"].flush_user.assert_awaited_once_with(user_id=USER_ID)
    assert "3" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unrelated_callbacks_fall_through() -> None:
    assert not await handle_delivery_menu(_query(), _context(), AsyncMock(), USER_ID, "edit_3")
