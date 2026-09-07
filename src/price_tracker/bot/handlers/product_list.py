"""`/list` — an index of products, and one screen per product.

It began as one message per product, which buried the chat. The reply to that
was a single message holding an index, one selected product's card and its
action buttons — but the buttons acted on whichever product the index had
marked with a `▸`, so a screen of seven button rows never said what any of them
would touch.

Two screens instead. The index is one button per product, ten to a page, each
carrying its price. Tapping one opens that product: its card, and actions that
can only mean the product named above them.

Rendering is a pure function of its inputs, so the command and the callbacks
build the same screens and the tests assert on them without a Telegram round
trip.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.decorators import _convert_display, _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_relative_time,
    _format_threshold,
    _safe_dec,
)
from price_tracker.bot.keyboards import (
    LIST_GOTO_PREFIX,
    PRODUCT_PREFIX,
    close_button,
    nav_row,
)
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import push_nav

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# user_data key holding the open listing's message id, so a plain number typed
# in the chat can jump the listing instead of being ignored.
LIST_MESSAGE_KEY = "list_message_id"

# One page of the index. Ten buttons is a screen you can read; the rest are a tap
# away, which is cheaper than a wall.
PAGE_SIZE = 10

# The index is a list, so a name is budgeted to keep each row scannable. The
# product's own screen shows it whole.
BUTTON_NAME_BUDGET = 32


def _product_card(product: dict[str, Any]) -> list[str]:
    """The full detail block for the product currently being shown."""
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    pid = product["id"]
    url = product.get("url", "")
    current = _safe_dec(product.get("current_price"))
    initial = _safe_dec(product.get("initial_price"))
    target = _safe_dec(product.get("target_price"))
    lowest = _safe_dec(product.get("lowest_price"))
    currency = product.get("currency", "") or detect_currency(url) or "EUR"
    price_str = _convert_display(current, currency) if current else _("N/A")

    # The shop is part of the name now, so it is not repeated as a field.
    lines = [
        f"<b>#{pid}</b> {_escape_html(product_label(product))}",
        f"💰 {price_str}",
    ]

    if initial and current and initial != current and initial > 0:
        diff = (initial - current) / initial * 100
        if diff > 0:
            lines.append(
                _("📌 Initial price: €{initial:.2f} (<i>-{diff:.1f}% since tracking</i>)").format(
                    initial=initial, diff=diff
                )
            )
        else:
            lines.append(
                _(
                    "📈 Initial price: €{initial:.2f} (<i>+{increase:.1f}% since tracking</i>)"
                ).format(initial=initial, increase=abs(diff))
            )

    if lowest and current and lowest < current:
        lines.append(_("📉 Min: €{price:.2f}").format(price=lowest))

    threshold = _format_threshold(
        product.get("threshold_type", "percentage"),
        product.get("threshold_value", "10"),
    )
    lines.append(_("🎯 Threshold: {threshold}").format(threshold=threshold))
    if target:
        lines.append(_("🏁 Target: €{price:.2f}").format(price=target))

    custom_interval = product.get("check_interval_minutes")
    if custom_interval:
        if custom_interval >= 60:
            hours = custom_interval / 60
            interval = f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h"
        else:
            interval = f"{custom_interval}min"
        lines.append(_("🔄 Check: every {interval}").format(interval=interval))

    ago = _format_relative_time(product.get("last_checked_at"))
    if ago:
        lines.append(_("🕐 Last check: {ago}").format(ago=ago))

    errors = product.get("consecutive_errors", 0)
    if errors and errors > 0:
        lines.append(
            _("⚠️ {count} failed reads recently — details with /errors").format(count=errors)
        )

    return lines


def page_count(total: int) -> int:
    """How many pages `total` products fill, never fewer than one."""
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def build_index_view(
    products: Sequence[dict[str, Any]],
    page: int = 0,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
    extra_rows: Sequence[list[InlineKeyboardButton]] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    """One page of the index: a button per product, carrying its price.

    `page` is clamped, so a stale button from a listing whose products have since
    been deleted lands on a page that exists.

    `context`/`message_id` are only needed for the exit row: an index reached from
    the menu offers ◀️ Back, one opened by /list has nowhere to go back to.
    """
    exits = (nav_row(context, message_id) if context is not None else [close_button()]) or [
        close_button()
    ]

    if not products:
        return (
            _("📭 You have no tracked products.\nPaste me a link to get started!"),
            InlineKeyboardMarkup([*extra_rows, exits]),
        )

    pages = page_count(len(products))
    current = max(0, min(page, pages - 1))
    shown = products[current * PAGE_SIZE : (current + 1) * PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                _index_label(product), callback_data=f"{PRODUCT_PREFIX}{product['id']}"
            )
        ]
        for product in shown
    ]

    if pages > 1:
        # Only the arrows that lead somewhere. A greyed-out one still looks
        # tappable, and tapping it does nothing — which reads as a broken button
        # rather than as the end of the list.
        pager = []
        if current > 0:
            pager.append(
                InlineKeyboardButton("◀", callback_data=f"{LIST_GOTO_PREFIX}{current - 1}")
            )
        pager.append(
            InlineKeyboardButton(
                f"{current + 1}/{pages}", callback_data=f"{LIST_GOTO_PREFIX}{current}"
            )
        )
        if current < pages - 1:
            pager.append(
                InlineKeyboardButton("▶", callback_data=f"{LIST_GOTO_PREFIX}{current + 1}")
            )
        rows.append(pager)

    text = _("<b>📦 Your products ({count})</b>").format(count=len(products))
    if pages > 1:
        text += _("\n\nPage {page} of {pages} — tap a product to open it.").format(
            page=current + 1, pages=pages
        )
    else:
        text += _("\n\nTap a product to open it.")

    return text, InlineKeyboardMarkup([*rows, *extra_rows, exits])


def _index_label(product: dict[str, Any]) -> str:
    """`#3 Name · shop — €429.00`, sized to stay one readable row."""
    price = _safe_dec(product.get("current_price"))
    tail = f" — €{price:.2f}" if price else ""
    return f"#{product['id']} {product_label(product, BUTTON_NAME_BUDGET)}{tail}"


def build_product_view(
    product: dict[str, Any],
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """One product: its card, and actions that can only mean the product above them."""
    exits = (nav_row(context, message_id) if context is not None else [close_button()]) or [
        close_button()
    ]
    pid = product["id"]

    first_row = [
        InlineKeyboardButton(_("🔍 Check"), callback_data=f"check_{pid}"),
        InlineKeyboardButton(_("📊 Price history"), callback_data=f"chart_{pid}"),
    ]
    url = product.get("url", "")
    if url:
        first_row.insert(0, InlineKeyboardButton(_("🔗 Open"), url=url))

    rows = [
        first_row,
        [
            InlineKeyboardButton(_("⏸ Pause"), callback_data=f"pause_{pid}"),
            InlineKeyboardButton(_("🗑 Delete"), callback_data=f"remove_{pid}"),
            InlineKeyboardButton(_("✏️ Edit"), callback_data=f"edit_{pid}"),
        ],
        exits,
    ]
    return "\n".join(_product_card(product)), InlineKeyboardMarkup(rows)


@with_locale
@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's tracked products as one paginated message.

    Only ever one: a second /list used to leave the first listing behind as a
    dead panel whose buttons still worked, which is exactly the chat clutter the
    single-message listing was built to remove.
    """
    db = _db(context)
    await _close_open_listing(update, context)
    products = await db.get_active_products(update.effective_user.id)
    text, keyboard = build_index_view(products, 0)
    message = await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    if context.user_data is not None:
        context.user_data[LIST_MESSAGE_KEY] = message.message_id
        push_nav(context, message.message_id, f"{LIST_GOTO_PREFIX}0")


async def _close_open_listing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the listing already on screen, if there is one and it still exists."""
    if context.user_data is None:
        return
    message_id = context.user_data.pop(LIST_MESSAGE_KEY, None)
    if message_id is None:
        return
    with contextlib.suppress(TelegramError):
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=message_id)


def register(app: Application) -> None:
    """Register the /lista command handlers on `app`."""
    app.add_handler(CommandHandler("lista", cmd_list))
    app.add_handler(CommandHandler("list", cmd_list))
