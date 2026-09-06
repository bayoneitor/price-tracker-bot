"""The three ways out of a screen: back, close, cancel.

These are handled once, here, rather than per view: closing means deleting
whichever message carries the button, cancelling means dropping whatever the bot
was waiting for, and going back means re-rendering the screen this one was opened
from — all the same work everywhere.

Going back re-dispatches the previous callback token through the normal chain, so
a returning screen is rebuilt from current data rather than restored from a
snapshot: prices that moved while the user was away show as they are now.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram.error import BadRequest, TelegramError

from price_tracker.bot.keyboards import BACK_CALLBACK, CANCEL_CALLBACK, CLOSE_CALLBACK
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import (
    clear_pending,
    forget_nav,
    pop_nav,
    push_nav,
    transfer_nav,
)

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Where "back" lands when the trail is gone — after a restart, say, since the
# trail lives in memory.
FALLBACK_SCREEN = "menu_main"


class ReplyAsEdit:
    """Renders a panel as a *new* message instead of editing the current one.

    Every screen builder speaks `edit_message_text`, because every screen normally
    replaces the panel it was opened from. One case cannot: Telegram will not turn
    a photo message back into a text one, so leaving a chart has to send the panel
    rather than edit it. This adapter lets the same builders serve both without a
    second rendering path.
    """

    def __init__(self, query: Any) -> None:
        self._query = query
        self.sent: Any = None

    @property
    def message(self) -> Any:
        return self._query.message

    @property
    def from_user(self) -> Any:
        return self._query.from_user

    @property
    def data(self) -> Any:
        return self._query.data

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        kwargs.pop("disable_web_page_preview", None)
        self.sent = await self._query.message.reply_text(
            text, disable_web_page_preview=True, **kwargs
        )
        return self.sent

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any:
        return None


async def handle_close(query: Any, context: ContextTypes.DEFAULT_TYPE, data: str) -> bool:
    """Dismiss whichever message carries the button."""
    if data != CLOSE_CALLBACK:
        return False
    message_id = getattr(getattr(query, "message", None), "message_id", None)
    if message_id is not None:
        forget_nav(context, message_id)
        _forget_listing(context, message_id)
    try:
        await query.message.delete()
    except BadRequest as exc:
        # A bot may only delete its own messages for 48 hours. Past that, collapse
        # in place rather than leaving a dead keyboard behind.
        logger.debug("Could not delete message, collapsing instead: %s", exc)
        await query.edit_message_text(_("👍 Closed. Send /menu to open it again."))
    return True


async def handle_cancel(query: Any, context: ContextTypes.DEFAULT_TYPE, data: str) -> bool:
    """Drop the answer the bot was waiting for, without losing the screen."""
    if data != CANCEL_CALLBACK:
        return False
    clear_pending(context)
    await query.edit_message_text(_("👍 Cancelled — nothing was changed."))
    return True


def _message_id(query: Any) -> int | None:
    return getattr(getattr(query, "message", None), "message_id", None)


def _holds_text(message: Any) -> bool:
    """Whether this message can be edited into a panel.

    A photo message cannot: Telegram will not replace media with text. Leaving a
    chart therefore has to send the panel as a new message.
    """
    return getattr(message, "text", None) is not None


async def handle_back(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    db: Any,
    user_id: int,
    data: str,
    dispatch: Any,
) -> bool:
    """Re-render the screen this one was opened from."""
    if data != BACK_CALLBACK:
        return False

    message_id = _message_id(query)
    target = (pop_nav(context, message_id) if message_id is not None else None) or FALLBACK_SCREEN

    if message_id is None or _holds_text(query.message):
        handled = await dispatch(query, context, db, user_id, target)
        if handled and message_id is not None:
            push_nav(context, message_id, target)
        return handled

    # Coming back from a chart: the photo stays as a record, stripped of buttons
    # so it cannot be pressed twice, and the panel reopens below it.
    renderer = ReplyAsEdit(query)
    handled = await dispatch(renderer, context, db, user_id, target)
    if renderer.sent is not None:
        transfer_nav(context, message_id, renderer.sent.message_id)
        push_nav(context, renderer.sent.message_id, target)
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
    return handled


def _forget_listing(context: ContextTypes.DEFAULT_TYPE, closed_id: int) -> None:
    """Drop the open-listing reference, but only if it was the listing we closed.

    The same button dismisses other panels; forgetting unconditionally would stop
    a typed index number from steering a listing that is still on screen.
    """
    from price_tracker.bot.handlers.product_list import LIST_MESSAGE_KEY  # noqa: PLC0415

    if context.user_data is None:
        return
    if context.user_data.get(LIST_MESSAGE_KEY) == closed_id:
        context.user_data.pop(LIST_MESSAGE_KEY, None)
