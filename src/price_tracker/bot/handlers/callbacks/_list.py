"""Callbacks for the paginated `/list` view.

The view carries no server-side state — the selected index travels in the
callback data — so a listing keeps working after a restart, and two devices
looking at the same listing never fight over a cursor.

This is also what the menu's 📦 Products entry opens. The menu used to hold four
separate "pick a product" screens, each capped at eight or ten products and none
of them able to page, jump or close, while the listing that could do all of it was
only reachable once you had more than ten.

Closing is not handled here but in `_nav`, with the other ways out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest

from price_tracker.bot.handlers._helpers import _get_user_product, _parse_id
from price_tracker.bot.handlers.product_list import (
    LIST_MESSAGE_KEY,
    build_index_view,
    build_product_view,
)
from price_tracker.bot.keyboards import LIST_GOTO_PREFIX, PRODUCT_PREFIX
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def _extra_rows(db: Any, user_id: int) -> list[list[InlineKeyboardButton]]:
    """What sits between the product's own buttons and the way out.

    Adding a product has no other affordance — the listing only ever hinted at it
    when it was empty — and paused products are invisible here, the listing being
    the active ones.
    """
    row = [InlineKeyboardButton(_("➕ Add product"), callback_data="menu_add")]
    paused = [p for p in await db.get_all_products(user_id) if not p.get("is_active")]
    if paused:
        row.append(
            InlineKeyboardButton(
                _("⏸ {count} paused").format(count=len(paused)), callback_data="menu_paused"
            )
        )
    return [row]


async def handle_list_navigation(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle `list_go_<page>` and `prod_<id>`. True when handled."""
    if data.startswith(PRODUCT_PREFIX):
        return await _open_product(query, context, user_id, data)
    if not data.startswith(LIST_GOTO_PREFIX):
        return False

    page = _parse_id(data.removeprefix(LIST_GOTO_PREFIX))
    if page is None or page < 0:
        # Tampered callback data: start from the first page rather than fail.
        page = 0

    # Re-read: the index may be minutes old and products deleted since.
    # build_index_view clamps the page, so a stale button lands on one that exists.
    products = await db.get_active_products(user_id)
    message_id = getattr(getattr(query, "message", None), "message_id", None)
    text, keyboard = build_index_view(
        products,
        page,
        context=context,
        message_id=message_id,
        extra_rows=await _extra_rows(db, user_id),
    )
    await _render(query, text, keyboard)

    # Whichever message is showing the index is the one a typed id opens. Only
    # /list used to record this, so an index opened from the menu ignored the
    # very numbers its buttons put on screen.
    if context.user_data is not None and message_id is not None:
        context.user_data[LIST_MESSAGE_KEY] = message_id
    return True


async def _open_product(
    query: Any, context: ContextTypes.DEFAULT_TYPE, user_id: int, data: str
) -> bool:
    """Open one product's own screen, where the buttons can only mean it."""
    product_id = _parse_id(data.removeprefix(PRODUCT_PREFIX))
    if product_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    product = await _get_user_product(context, product_id, user_id)
    if not product:
        await query.edit_message_text(_("❌ Product not found."))
        return True

    message_id = getattr(getattr(query, "message", None), "message_id", None)
    text, keyboard = build_product_view(product, context=context, message_id=message_id)
    await _render(query, text, keyboard)
    return True


async def _render(query: Any, text: str, keyboard: Any) -> None:
    """Draw a screen, tolerating a re-render of what is already on it."""
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
