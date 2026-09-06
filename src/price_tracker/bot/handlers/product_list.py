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
from price_tracker.bot.keyboards import LIST_GOTO_PREFIX, close_button, nav_row
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import push_nav
from price_tracker.core.textlimits import SAFE_LIMIT

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# user_data key holding the open listing's message id, so a plain number typed
# in the chat can jump the listing instead of being ignored.
LIST_MESSAGE_KEY = "list_message_id"

# Names are shown whole. Telegram's hard cap is 4096 characters for the whole
# message, which twenty ordinary names come nowhere near — but a listing of
# Amazon titles can, so `_fits` re-renders the index under this budget rather
# than letting the send fail. Long lists are elided rather than truncating the
# card.
INDEX_NAME_BUDGET = 38
MAX_INDEX_ROWS = 20
# Direct-jump buttons, 5 per row. Beyond this a window around the current
# product is shown instead — 100 buttons is unusable and Telegram caps rows.
JUMP_BUTTONS_PER_ROW = 5
MAX_JUMP_BUTTONS = 10


def _index_block(
    products: Sequence[dict[str, Any]], current: int, *, budget: int | None = None
) -> list[str]:
    """The 'all your products' index, with the selected row marked."""
    lines: list[str] = []
    shown = products[:MAX_INDEX_ROWS]
    for position, product in enumerate(shown):
        row = f"{position + 1}. {_escape_html(product_label(product, budget))}"
        lines.append(f"<b>▸ {row}</b>" if position == current else f"   {row}")
    hidden = len(products) - len(shown)
    if hidden > 0:
        lines.append(_("   … and {count} more").format(count=hidden))
    return lines


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
    products: Sequence[dict[str, Any]],
    index: int,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
    extra_rows: Sequence[list[InlineKeyboardButton]] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the whole listing for `products` with `index` selected.

    `index` is clamped, so a stale button from a listing whose products have
    since been deleted lands on a valid product instead of raising.

    `context`/`message_id` are only needed for the exit row: a listing reached
    from the menu offers ◀️ Back, one opened by /list has nowhere to go back to.

    `extra_rows` go above the exits — "add a product", "N paused". They are passed
    in rather than worked out here so this stays a pure function of the products
    it is given.
    """
    exits = (nav_row(context, message_id) if context is not None else [close_button()]) or [
        close_button()
    ]

    if not products:
        return (
            _("📭 You have no tracked products.\nPaste me a link to get started!"),
            InlineKeyboardMarkup([*extra_rows, exits]),
        )

    current = max(0, min(index, len(products) - 1))
    product = products[current]

    def render(budget: int | None) -> str:
        return "\n".join(
            [
                _("<b>📦 Your products ({count})</b>").format(count=len(products)),
                "",
                *_index_block(products, current, budget=budget),
                "",
                "───────────────",
                *_product_card(product),
            ]
        )

    # Whole names unless they would cost the send: a message over the limit is
    # rejected outright, which is worse than an abbreviated index.
    text = render(None)
    if len(text) > SAFE_LIMIT:
        text = render(INDEX_NAME_BUDGET)

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
    rows.extend(extra_rows)
    rows.append(exits)

    return text, InlineKeyboardMarkup(rows)


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
    text, keyboard = build_list_view(products, 0)
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
