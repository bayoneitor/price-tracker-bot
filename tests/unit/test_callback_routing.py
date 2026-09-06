"""The three exits are handled before any screen sees them.

Close, cancel and back are resolved by the dispatcher itself, not by the screen
that drew the button — otherwise every new screen would have to remember to pass
them through, and the one that forgot would trap the user on itself.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers import callbacks
from price_tracker.bot.keyboards import BACK_CALLBACK, CANCEL_CALLBACK, CLOSE_CALLBACK
from price_tracker.bot.navigation import push_nav


def _context() -> MagicMock:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"db": db}
    return context


def _update(data: str) -> MagicMock:
    query = MagicMock(message=MagicMock(message_id=5, text="panel", delete=AsyncMock()))
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    query.data = data
    query.from_user.id = 1
    update = MagicMock(callback_query=query)
    update.effective_user.language_code = "en"
    return update


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [CLOSE_CALLBACK, CANCEL_CALLBACK])
async def test_close_and_cancel_never_reach_a_screen(
    data: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched: list[str] = []

    async def spy(query: Any, context: Any, db: Any, user_id: int, token: str) -> bool:
        dispatched.append(token)
        return True

    monkeypatch.setattr(callbacks, "_dispatch", spy)

    await callbacks.handle_callback(_update(data), _context())

    assert dispatched == []


@pytest.mark.asyncio
async def test_back_dispatches_the_previous_screen_not_the_back_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[str] = []

    async def spy(query: Any, context: Any, db: Any, user_id: int, token: str) -> bool:
        dispatched.append(token)
        return True

    monkeypatch.setattr(callbacks, "_dispatch", spy)
    context = _context()
    push_nav(context, 5, "menu_main")
    push_nav(context, 5, "menu_prodotti")

    await callbacks.handle_callback(_update(BACK_CALLBACK), context)

    assert dispatched == ["menu_main"]


@pytest.mark.asyncio
async def test_an_unauthorized_click_is_dropped_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[str] = []

    async def spy(query: Any, context: Any, db: Any, user_id: int, token: str) -> bool:
        dispatched.append(token)
        return True

    monkeypatch.setattr(callbacks, "_dispatch", spy)
    context = _context()
    context.bot_data["db"].is_user_allowed = AsyncMock(return_value=False)

    await callbacks.handle_callback(_update("menu_main"), context)

    assert dispatched == []


@pytest.mark.asyncio
async def test_a_handled_screen_is_recorded_on_the_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is what makes ◀️ Back work without every screen naming its own parent."""

    async def spy(query: Any, context: Any, db: Any, user_id: int, token: str) -> bool:
        return True

    monkeypatch.setattr(callbacks, "_dispatch", spy)
    context = _context()

    await callbacks.handle_callback(_update("menu_prodotti"), context)

    assert context.user_data["nav"][5] == ["menu_prodotti"]


@pytest.mark.asyncio
async def test_an_unhandled_callback_leaves_no_trace_on_the_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token is recorded before the screen renders, so a miss must undo it."""

    async def spy(query: Any, context: Any, db: Any, user_id: int, token: str) -> bool:
        return False

    monkeypatch.setattr(callbacks, "_dispatch", spy)
    context = _context()
    push_nav(context, 5, "menu_main")

    await callbacks.handle_callback(_update("nonsense_9"), context)

    assert context.user_data["nav"][5] == ["menu_main"]
