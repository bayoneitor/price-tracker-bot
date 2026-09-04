"""Callbacks for the paginated `/list` view: page, jump, close.

The view carries no server-side state — the selected index travels in the
callback data — so a listing keeps working after a restart, and two devices
looking at the same listing never fight over a cursor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram.constants import ParseMode
from telegram.error import BadRequest

from price_tracker.bot.handlers._helpers import _parse_id
from price_tracker.bot.handlers.product_list import (
    LIST_CLOSE,
    LIST_GOTO_PREFIX,
    LIST_MESSAGE_KEY,
    build_list_view,
)
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def handle_list_navigation(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle `list_go_<n>` and `list_close`. Returns True when handled."""
    if data == LIST_CLOSE:
        await _close(query)
        if context.user_data is not None:
            context.user_data.pop(LIST_MESSAGE_KEY, None)
        return True

    if not data.startswith(LIST_GOTO_PREFIX):
        return False

    index = _parse_id(data.removeprefix(LIST_GOTO_PREFIX))
    if index is None or index < 0:
        # Tampered callback data: re-render from the start rather than fail.
        index = 0

    # Re-read: the listing may be minutes old and products deleted since.
    # build_list_view clamps the index, so a stale button lands somewhere valid.
    products = await db.get_active_products(user_id)
    text, keyboard = build_list_view(products, index)
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        # Tapping the current page number re-renders identical content, which
        # Telegram rejects. Nothing is wrong and the user sees what they asked.
        if "message is not modified" not in str(exc).lower():
            raise
    return True


async def _close(query: Any) -> None:
    """Dismiss the listing, leaving nothing behind when possible."""
    try:
        await query.message.delete()
    except BadRequest as exc:
        # A bot may only delete its own messages for 48 hours. Past that,
        # collapse in place rather than leaving a dead keyboard.
        logger.debug("Could not delete list message, collapsing instead: %s", exc)
        await query.edit_message_text(_("📦 Listing closed — send /list to open it again."))
