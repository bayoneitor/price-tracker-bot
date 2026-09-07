"""`/list` — an index of products, and one screen per product.

It began as one message per product, which buried the chat. The reply to that
was a single message holding an index, one selected product's card and its
action buttons — but the buttons acted on whichever product the index had
marked with a `▸`, so a screen of seven button rows never said what any of them
would touch.

Two screens instead. The index is a written list — six products to a page, each
with its shop and its price — under a grid of the numbers those lines carry.
Tapping a number opens that product; the grid replaced a cursor you had to step
product by product, which cost six taps to reach the seventh. The product's own
screen is where the actions live, under the name they apply to.

Rendering is a pure function of its inputs, so the command and the callbacks
build the same screens and the tests assert on them without a Telegram round
trip.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    PICKER_PREFIX,
    PRODUCT_PREFIX,
    close_button,
    nav_row,
)
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import push_nav
from price_tracker.core.price_stats import (
    AVERAGE_WINDOWS,
    averages_over_windows,
    readings_from_records,
    time_weighted_average,
)
from price_tracker.core.textlimits import SAFE_LIMIT, truncate_visible
from price_tracker.core.url_utils import extract_amazon_asin

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# user_data key holding the open listing's message id, so a plain number typed
# in the chat can jump the listing instead of being ignored.
LIST_MESSAGE_KEY = "list_message_id"

# The index pages six at a time. Nine fit a tidier 3x3 grid, but a page of nine
# untouched shop titles wraps to two or three lines apiece — the written list
# alone ran past what a phone shows without scrolling, before the grid and the
# exit row even entered the chat.
INDEX_PAGE_SIZE = 6

# A picker still pages ten. Its buttons carry a name rather than a number, so
# they stack in a column and the grid's geometry does not apply.
PAGE_SIZE = 10

# Names are written out whole. Ten of them come nowhere near Telegram's 4096
# characters — but a page of untouched Amazon titles can, so the index falls back
# to this budget rather than letting the send fail. Naming a product yourself is
# the better answer, which is what the alias is for.
INDEX_NAME_BUDGET = 46

# A picker's name lives in a button, which wraps rather than truncating: three
# lines per product is the wall this screen exists to avoid.
PICKER_NAME_BUDGET = 30


def _product_card(product: dict[str, Any], summary: PriceSummary | None = None) -> list[str]:
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

    # The summary carries the dated floor and every window's average; without
    # one (a caller that has no db to ask) the card still states the floor it
    # already holds on the product row.
    if summary is not None and not summary.is_empty():
        lines.extend(summary_lines(summary, currency))
        floor = summary.lowest
        if floor is not None and current is not None and floor >= current:
            lines.append(_("✨ This is the cheapest it has ever been."))
    elif lowest:
        lines.append(_("📉 Lowest ever: {price}").format(price=_convert_display(lowest, currency)))

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


def _page_ids(products: Sequence[dict[str, Any]], position: int) -> list[int]:
    """The ids on the page `position` lands on — the only ones about to render."""
    if not products:
        return []
    current = max(0, min(position, len(products) - 1))
    page = current // INDEX_PAGE_SIZE
    shown = list(products)[page * INDEX_PAGE_SIZE : (page + 1) * INDEX_PAGE_SIZE]
    return [int(p["id"]) for p in shown]


def page_count(total: int, size: int = PAGE_SIZE) -> int:
    """How many pages `total` products fill, never fewer than one."""
    return max(1, (total + size - 1) // size)


def build_index_view(
    products: Sequence[dict[str, Any]],
    position: int = 0,
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
    extra_rows: Sequence[list[InlineKeyboardButton]] = (),
    averages: Mapping[int, Decimal] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """The written index, and a grid of numbers that open what it lists.

    `position` is an absolute index into `products`; the page follows from it, so
    one number travels in the callback data rather than two that could disagree.
    It is clamped, so a stale button from an index whose products have since been
    deleted lands on a page that exists.

    The numbers on the buttons are the numbers on the lines — product ids, the
    same thing typing `7` into the chat opens. Nothing here has to be inferred
    from a position on screen.

    `context`/`message_id` are only needed for the exit row: an index reached from
    the menu offers Back, one opened by /list has nowhere to go back to.
    """
    exits = nav_row(context, message_id, close=False) if context is not None else []
    exits = [
        *exits,
        InlineKeyboardButton(_("\u2795 Add"), callback_data="menu_add"),
        close_button(),
    ]

    if not products:
        return (
            _("\U0001f4ed You have no tracked products.\nPaste me a link to get started!"),
            InlineKeyboardMarkup([*extra_rows, exits]),
        )

    total = len(products)
    current = max(0, min(position, total - 1))
    pages = page_count(total, INDEX_PAGE_SIZE)
    page = current // INDEX_PAGE_SIZE
    shown = list(products)[page * INDEX_PAGE_SIZE : (page + 1) * INDEX_PAGE_SIZE]

    def render(budget: int | None) -> str:
        head = [_("<b>\U0001f4e6 Your products ({count})</b>").format(count=total)]
        if pages > 1:
            head.append(_("Page {page} of {pages}").format(page=page + 1, pages=pages))
        # A blank line between entries, not just between lines: a name long
        # enough to wrap runs into the next product otherwise, and every line
        # starts with a number, so there is nothing else to tell them apart.
        entries = []
        for product in shown:
            name = _escape_html(product_label(product, budget))
            entry = f"<b>#{product['id']}</b> {name}"
            # The numbers go on their own line: three of them appended to a
            # shop title long enough to wrap already is how the entry stops
            # being readable at a glance.
            stats = _stats_line(product, (averages or {}).get(int(product["id"])))
            entry += f"\n{stats}" if stats else _price_tag(product)
            entries.append(entry)
        return "\n".join(head) + "\n\n" + "\n\n".join(entries)

    text = render(None)
    if len(text) > SAFE_LIMIT:
        text = render(INDEX_NAME_BUDGET)

    rows = _number_grid(shown)
    if pages > 1:
        # Its own row, and labelled as pages: the grid above moves nowhere, so the
        # two controls can never be read as the same one.
        rows.append(_page_row(page, pages, prefix=LIST_GOTO_PREFIX))
    return text, InlineKeyboardMarkup([*rows, *extra_rows, exits])


def _number_grid(shown: Sequence[dict[str, Any]]) -> list[list[InlineKeyboardButton]]:
    """The page's products as a grid of their own numbers, three to a row.

    Three: wider and Telegram shrinks the labels, narrower and the grid is a
    column again. A short label is the point — the name is on the line above,
    where it has the room to be read. A full page is two rows of it.
    """
    buttons = [
        InlineKeyboardButton(f"#{product['id']}", callback_data=f"{PRODUCT_PREFIX}{product['id']}")
        for product in shown
    ]
    return [buttons[i : i + 3] for i in range(0, len(buttons), 3)]


def _page_row(current: int, pages: int, *, prefix: str) -> list[InlineKeyboardButton]:
    """The page control. Wraps, so it keeps its shape at both ends — an arrow
    that vanishes moves every button beside it."""
    return [
        InlineKeyboardButton("⏪", callback_data=f"{prefix}{(current - 1) % pages}"),
        InlineKeyboardButton(
            _("Page {page}/{pages}").format(page=current + 1, pages=pages),
            callback_data=f"{prefix}{current}",
        ),
        InlineKeyboardButton("⏩", callback_data=f"{prefix}{(current + 1) % pages}"),
    ]


def _price_tag(product: dict[str, Any]) -> str:
    price = _safe_dec(product.get("current_price"))
    return f" — €{price:.2f}" if price else ""


# "Recently" for the average shown beside a price. Long enough to survive a
# single promotion week, short enough that a year-old price cannot drag it.
AVERAGE_WINDOW_DAYS = 30


async def recent_averages(
    db: Any, product_ids: Sequence[int], *, days: int = AVERAGE_WINDOW_DAYS
) -> dict[int, Decimal]:
    """The time-weighted average price of each product over the last `days`.

    One query for the whole page, not one per product: the listing renders six
    at a time and six round trips to draw one screen is how a listing gets
    slow. Reuses `get_price_change_points`, which already collapses unchanged
    runs in SQL — a month of checks every 30 minutes is ~1400 rows and maybe
    three actual prices.

    Never raises: an average is a nicety on a screen whose job is showing
    products, and a failure to compute one must not cost the listing itself.
    """
    if not product_ids:
        return {}
    until = datetime.now(UTC)
    since = (until - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    averages: dict[int, Decimal] = {}
    try:
        histories = await db.get_price_change_points(list(product_ids), since=since)
        for product_id, records in histories.items():
            average = time_weighted_average(readings_from_records(records), until=until)
            if average is not None:
                averages[int(product_id)] = average
    except Exception:  # noqa: BLE001 — the listing matters, the average does not
        # The whole computation, not just the query: a repository handing back
        # a shape this did not expect would otherwise take the listing down
        # with it, which is precisely what "never raises" is supposed to mean.
        logger.exception("Could not compute the %d-day average", days)
        return {}
    return averages


@dataclass(frozen=True, slots=True)
class PriceSummary:
    """What a product's history says, for the card and every chart caption.

    One object rendered by one function, so the product screen, the price
    chart and the backfill chart cannot drift into saying different things
    about the same product.
    """

    lowest: Decimal | None = None
    lowest_at: datetime | None = None
    averages: Mapping[int, Decimal] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return self.lowest is None and not self.averages


async def price_summary(db: Any, product: dict[str, Any]) -> PriceSummary:
    """The floor with its date, and one average per window. Never raises.

    Two queries: the change points of the longest window (which serves every
    shorter one — the 30-day average is computed from the same rows, clamped)
    and the single cheapest row ever recorded, which can be far older than any
    window and so cannot come from the same fetch.

    Falls back to `products.lowest_price` when history holds no rows yet: a
    product added minutes ago knows its floor without having recorded one.
    """
    product_id = int(product["id"])
    until = datetime.now(UTC)
    longest = max(AVERAGE_WINDOWS)
    lowest = _safe_dec(product.get("lowest_price"))
    lowest_at: datetime | None = None
    averages: Mapping[int, Decimal] = {}

    try:
        since = (until - timedelta(days=longest)).strftime("%Y-%m-%d %H:%M:%S")
        histories = await db.get_price_change_points([product_id], since=since)
        readings = readings_from_records(histories.get(product_id, ()))
        averages = averages_over_windows(readings, until=until)

        floor = await db.lowest_price_point(product_id)
        # The recorded floor wins over the products column only when it is at
        # least as low: the column can be seeded from initial_price at add
        # time, before any row exists to date it.
        if floor is not None and (lowest is None or floor.price <= lowest):
            lowest = floor.price
            lowest_at = _parse_history_ts(floor.checked_at)
    except Exception:  # noqa: BLE001 — a summary is a nicety, the screen is not
        logger.exception("Could not summarise history for product %s", product_id)
        return PriceSummary(lowest=lowest)

    return PriceSummary(lowest=lowest, lowest_at=lowest_at, averages=averages)


def _parse_history_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def summary_lines(summary: PriceSummary, currency: str) -> list[str]:
    """The floor and the averages, as the card and every caption render them."""
    lines: list[str] = []
    if summary.lowest is not None:
        price = _convert_display(summary.lowest, currency)
        if summary.lowest_at is not None:
            lines.append(
                _("📉 Lowest ever: {price} ({date})").format(
                    price=price, date=summary.lowest_at.strftime("%Y-%m-%d")
                )
            )
        else:
            lines.append(_("📉 Lowest ever: {price}").format(price=price))
    if summary.averages:
        parts = [
            _("{days}d {price}").format(days=days, price=_convert_display(value, currency))
            for days, value in sorted(summary.averages.items())
        ]
        lines.append(_("📊 Average: {windows}").format(windows=" · ".join(parts)))
    return lines


def _stats_line(product: dict[str, Any], average: Decimal | None) -> str | None:
    """Current price, the floor it has ever hit, and the recent average.

    Returns None when there is no current price to anchor the others to — a
    product whose first check has not landed yet has nothing to compare.
    """
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    current = _safe_dec(product.get("current_price"))
    if not current:
        return None
    currency = product.get("currency", "") or detect_currency(product.get("url", "")) or "EUR"

    parts = [_("💰 {price}").format(price=_convert_display(current, currency))]
    lowest = _safe_dec(product.get("lowest_price"))
    if lowest:
        parts.append(_("📉 min {price}").format(price=_convert_display(lowest, currency)))
    if average is not None:
        parts.append(
            _("📊 {days}d avg {price}").format(
                days=AVERAGE_WINDOW_DAYS, price=_convert_display(average, currency)
            )
        )
    return " · ".join(parts)


def build_picker_view(
    products: Sequence[dict[str, Any]],
    page: int = 0,
    *,
    prefix: str,
    title: str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Choose one product: a page of buttons, and tapping one acts.

    Not the index's cursor. There you navigate; here you pick, and a cursor would
    cost two steps — move it, then confirm — for what is one tap.

    Each button's callback is `<prefix><id>`, so every action handler keeps
    receiving exactly what it received before: `check_3`, `settarget_3`,
    `grp_put_7_3`. Only the page turning is new.
    """
    exits = (nav_row(context, message_id) if context is not None else [close_button()]) or [
        close_button()
    ]
    if not products:
        return _("📭 You have no tracked products."), InlineKeyboardMarkup([exits])

    pages = page_count(len(products))
    current = max(0, min(page, pages - 1))
    shown = products[current * PAGE_SIZE : (current + 1) * PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                f"#{product['id']} {product_label(product, PICKER_NAME_BUDGET)}"
                f"{_price_tag(product)}",
                callback_data=f"{prefix}{product['id']}",
            )
        ]
        for product in shown
    ]
    if pages > 1:
        rows.append(_page_row(current, pages, prefix=f"{PICKER_PREFIX}{prefix}|"))

    # The same msgid the index uses, not one with the newlines baked in: a
    # near-identical string is a second thing to translate and a second thing to
    # forget.
    text = f"<b>{title}</b>"
    if pages > 1:
        text += "\n\n" + _("Page {page} of {pages}").format(page=current + 1, pages=pages)
    return text, InlineKeyboardMarkup([*rows, exits])


def build_product_view(
    product: dict[str, Any],
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
    summary: PriceSummary | None = None,
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

    rows = [first_row]
    # Amazon only, and only when the URL actually carries an ASIN — a search
    # results page or a cart link tracked by mistake has neither.
    if extract_amazon_asin(url):
        rows.append([InlineKeyboardButton(_("📈 Keepa graph"), callback_data=f"keepa_{pid}")])
    rows += [
        [
            InlineKeyboardButton(_("⏸ Pause"), callback_data=f"pause_{pid}"),
            InlineKeyboardButton(_("🗑 Delete"), callback_data=f"remove_{pid}"),
            InlineKeyboardButton(_("✏️ Edit"), callback_data=f"edit_{pid}"),
        ],
        exits,
    ]
    return "\n".join(_product_card(product, summary)), InlineKeyboardMarkup(rows)


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
    text, keyboard = build_index_view(
        products, 0, averages=await recent_averages(db, _page_ids(products, 0))
    )
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
