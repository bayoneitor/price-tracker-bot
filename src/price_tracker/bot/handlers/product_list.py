"""`/list` — an index of products, and one screen per product.

It began as one message per product, which buried the chat. The reply to that
was a single message holding an index, one selected product's card and its
action buttons — but the buttons acted on whichever product the index had
marked with a `▸`, so a screen of seven button rows never said what any of them
would touch.

Two screens instead. The index is a written list — ten products to a page, each
with its shop and its price, and the one under the cursor marked — and the
buttons move rather than act: one row steps the cursor product by product, a
second jumps a page at a time, and a third opens whatever the cursor is on. The
product's own screen is where the actions live, under the name they apply to.

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
    LIST_CURSOR_PREFIX,
    LIST_GOTO_PREFIX,
    PRODUCT_PREFIX,
    close_button,
    nav_row,
)
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import push_nav
from price_tracker.core.textlimits import SAFE_LIMIT, truncate_visible

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

# Names are written out whole. Ten of them come nowhere near Telegram's 4096
# characters — but a page of untouched Amazon titles can, so the index falls back
# to this budget rather than letting the send fail. Naming a product yourself is
# the better answer, which is what the alias is for.
INDEX_NAME_BUDGET = 46


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

    # With an alias on it, the label is the user's name for the product. The
    # shop's own goes here so a rename never hides what is actually tracked.
    if product.get("alias") and product.get("name"):
        lines.append(
            _("🏬 Listed as: {name}").format(
                name=_escape_html(truncate_visible(str(product["name"]), 70))
            )
        )

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
    cursor: int = 0,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
    extra_rows: Sequence[list[InlineKeyboardButton]] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    """The written index, with `cursor` marking the product the buttons act on.

    `cursor` is an absolute position in `products` and the page follows from it,
    so there is one piece of state travelling in the callback data rather than
    two that could disagree. It is clamped, so a stale button from an index whose
    products have since been deleted lands on one that exists.

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

    total = len(products)
    current = max(0, min(cursor, total - 1))
    pages = page_count(total)
    page = current // PAGE_SIZE
    shown = list(enumerate(products))[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    def render(budget: int | None) -> str:
        lines = [_("<b>📦 Your products ({count})</b>").format(count=total)]
        if pages > 1:
            lines.append(_("Page {page} of {pages}").format(page=page + 1, pages=pages))
        lines.append("")
        for position, product in shown:
            row = f"{_escape_html(product_label(product, budget))}{_price_tag(product)}"
            lines.append(
                f"<b>▸ #{product['id']} {row}</b>"
                if position == current
                else f"   #{product['id']} {row}"
            )
        return "\n".join(lines)

    text = render(None)
    if len(text) > SAFE_LIMIT:
        text = render(INDEX_NAME_BUDGET)

    selected = products[current]
    rows = [
        # Step: one product at a time. Wraps, so the row keeps its shape at both
        # ends — an arrow that vanishes moves everything beside it.
        [
            InlineKeyboardButton("◀", callback_data=f"{LIST_CURSOR_PREFIX}{(current - 1) % total}"),
            InlineKeyboardButton(
                f"{current + 1}/{total}", callback_data=f"{LIST_CURSOR_PREFIX}{current}"
            ),
            InlineKeyboardButton("▶", callback_data=f"{LIST_CURSOR_PREFIX}{(current + 1) % total}"),
        ],
        # Open: named, so nothing has to be inferred from the ▸ above.
        [
            InlineKeyboardButton(
                _("✅ Open #{pid}").format(pid=selected["id"]),
                callback_data=f"{PRODUCT_PREFIX}{selected['id']}",
            )
        ],
    ]
    if pages > 1:
        # Jump: a page at a time, and labelled as pages so the two rows cannot be
        # read as the same control.
        rows.append(
            [
                InlineKeyboardButton("⏪", callback_data=f"{LIST_GOTO_PREFIX}{(page - 1) % pages}"),
                InlineKeyboardButton(
                    _("Page {page}/{pages}").format(page=page + 1, pages=pages),
                    callback_data=f"{LIST_GOTO_PREFIX}{page}",
                ),
                InlineKeyboardButton("⏩", callback_data=f"{LIST_GOTO_PREFIX}{(page + 1) % pages}"),
            ]
        )

    return text, InlineKeyboardMarkup([*rows, *extra_rows, exits])


def _price_tag(product: dict[str, Any]) -> str:
    price = _safe_dec(product.get("current_price"))
    return f" — €{price:.2f}" if price else ""


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
