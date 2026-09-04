"""`/lista` handler — one paginated, closable view of the user's products.

Replaces the message-per-product listing, which buried the chat: a dozen
products meant a dozen messages that could not be collapsed or dismissed. The
whole listing is now a single message that is edited in place — an index of
every product on top, the selected product's full card below, and buttons to
page through them, jump straight to one, or close the whole thing.

Rendering is a pure function of `(products, index)`, so the command and the
pagination callback build the exact same view and the tests can assert on it
without a Telegram round trip.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.decorators import _convert_display, _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_relative_time,
    _format_threshold,
    _safe_dec,
)
from price_tracker.bot.messages import _
from price_tracker.core.textlimits import truncate_visible
from price_tracker.core.url_utils import store_label

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Callback prefixes owned by this view.
LIST_GOTO_PREFIX = "list_go_"
LIST_CLOSE = "list_close"

# user_data key holding the open listing's message id, so a plain number typed
# in the chat can jump the listing instead of being ignored.
LIST_MESSAGE_KEY = "list_message_id"

# Index-line budget. Telegram's hard cap is 4096 characters for the whole
# message; the index shares that with the product card and must not crowd it
# out, so long lists are elided rather than truncating the card.
INDEX_NAME_BUDGET = 38
MAX_INDEX_ROWS = 20
# Direct-jump buttons, 5 per row. Beyond this a window around the current
# product is shown instead — 100 buttons is unusable and Telegram caps rows.
JUMP_BUTTONS_PER_ROW = 5
MAX_JUMP_BUTTONS = 10


def _index_block(products: Sequence[dict[str, Any]], current: int) -> list[str]:
    """The 'all your products' index, with the selected row marked."""
    lines: list[str] = []
    shown = products[:MAX_INDEX_ROWS]
    for position, product in enumerate(shown):
        name = truncate_visible(product.get("name") or _("Unknown"), INDEX_NAME_BUDGET)
        row = f"{position + 1}. {_escape_html(name)}"
        lines.append(f"<b>▸ {row}</b>" if position == current else f"   {row}")
    hidden = len(products) - len(shown)
    if hidden > 0:
        lines.append(_("   … and {count} more").format(count=hidden))
    return lines


def _product_card(product: dict[str, Any]) -> list[str]:
    """The full detail block for the product currently being shown."""
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    pid = product["id"]
    name = product.get("name") or _("Unknown")
    url = product.get("url", "")
    current = _safe_dec(product.get("current_price"))
    initial = _safe_dec(product.get("initial_price"))
    target = _safe_dec(product.get("target_price"))
    lowest = _safe_dec(product.get("lowest_price"))
    currency = product.get("currency", "") or detect_currency(url) or "EUR"
    price_str = _convert_display(current, currency) if current else _("N/A")

    lines = [
        f"<b>#{pid}</b> {_escape_html(truncate_visible(name, 60))}",
        f"💰 {price_str}",
    ]

    store = store_label(url=url, domain=product.get("domain", ""))
    if store:
        lines.append(_("🌐 Store: {store}").format(store=_escape_html(store)))

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


def _jump_window(total: int, current: int) -> range:
    """Which product numbers get a direct-jump button.

    All of them while they fit; otherwise a window centred on the current one,
    clamped to the ends so the row keeps its width.
    """
    if total <= MAX_JUMP_BUTTONS:
        return range(total)
    start = max(0, min(current - MAX_JUMP_BUTTONS // 2, total - MAX_JUMP_BUTTONS))
    return range(start, start + MAX_JUMP_BUTTONS)


def build_list_view(
    products: Sequence[dict[str, Any]], index: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the whole listing for `products` with `index` selected.

    `index` is clamped, so a stale button from a listing whose products have
    since been deleted lands on a valid product instead of raising.
    """
    if not products:
        return (
            _("📭 You have no tracked products.\nPaste me a link to get started!"),
            InlineKeyboardMarkup([[InlineKeyboardButton(_("✖ Close"), callback_data=LIST_CLOSE)]]),
        )

    current = max(0, min(index, len(products) - 1))
    product = products[current]

    text = "\n".join(
        [
            _("<b>📦 Your products ({count})</b>").format(count=len(products)),
            "",
            *_index_block(products, current),
            "",
            "───────────────",
            *_product_card(product),
        ]
    )

    pid = product["id"]
    rows: list[list[InlineKeyboardButton]] = []

    if len(products) > 1:
        previous_index = (current - 1) % len(products)
        next_index = (current + 1) % len(products)
        rows.append(
            [
                InlineKeyboardButton("◀", callback_data=f"{LIST_GOTO_PREFIX}{previous_index}"),
                InlineKeyboardButton(
                    f"{current + 1}/{len(products)}",
                    callback_data=f"{LIST_GOTO_PREFIX}{current}",
                ),
                InlineKeyboardButton("▶", callback_data=f"{LIST_GOTO_PREFIX}{next_index}"),
            ]
        )

        jump: list[InlineKeyboardButton] = [
            InlineKeyboardButton(
                f"·{position + 1}·" if position == current else str(position + 1),
                callback_data=f"{LIST_GOTO_PREFIX}{position}",
            )
            for position in _jump_window(len(products), current)
        ]
        for start in range(0, len(jump), JUMP_BUTTONS_PER_ROW):
            rows.append(jump[start : start + JUMP_BUTTONS_PER_ROW])

    rows.append(
        [
            InlineKeyboardButton(_("🔍 Check"), callback_data=f"check_{pid}"),
            InlineKeyboardButton(_("📊 Price history"), callback_data=f"chart_{pid}"),
        ]
    )
    action_row = [
        InlineKeyboardButton(_("⏸ Pause"), callback_data=f"pause_{pid}"),
        InlineKeyboardButton(_("🗑 Delete"), callback_data=f"remove_{pid}"),
        InlineKeyboardButton(_("✏️ Edit"), callback_data=f"edit_{pid}"),
    ]
    url = product.get("url", "")
    if url:
        action_row.insert(0, InlineKeyboardButton(_("🔗 Open"), url=url))
    rows.append(action_row)
    rows.append([InlineKeyboardButton(_("✖ Close"), callback_data=LIST_CLOSE)])

    return text, InlineKeyboardMarkup(rows)


@with_locale
@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's tracked products as one paginated message."""
    db = _db(context)
    products = await db.get_active_products(update.effective_user.id)
    text, keyboard = build_list_view(products, 0)
    message = await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    if context.user_data is not None:
        context.user_data[LIST_MESSAGE_KEY] = message.message_id


def register(app: Application) -> None:
    """Register the /lista command handlers on `app`."""
    app.add_handler(CommandHandler("lista", cmd_list))
    app.add_handler(CommandHandler("list", cmd_list))
