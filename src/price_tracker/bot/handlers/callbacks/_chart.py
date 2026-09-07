"""The price chart and the ranges it can be drawn over (`chart_`, `chartr|`).

Split out of `_product.py`, which had no room left under its 500-LOC budget.

A chart opens showing everything the product remembers, because that is the
only window nobody has to guess at, and the row of ranges underneath narrows
it without leaving the message: the picture is swapped in place, so the chat
does not fill with charts of the same product at four different zooms.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InputFile, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import TelegramError

from price_tracker.bot.charts import generate_chart
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    resolve_owned_product,
)
from price_tracker.bot.keyboards import result_keyboard
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import forget_product, transfer_nav
from price_tracker.core.url_utils import extract_amazon_asin

if TYPE_CHECKING:
    from telegram import InlineKeyboardMarkup
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# `chart_<id>` opens the chart; `chartr|<id>|<range>` redraws it over another
# window. A separate token because the two do different things to the message:
# one replaces a panel with a photo, the other edits the photo in place.
CHART_RANGE_PREFIX = "chartr|"

# The window each range asks for, in days. `None` is "everything there is" —
# the default, and the only one that cannot mislead by omission.
CHART_RANGES: tuple[tuple[str, int | None], ...] = (
    ("30d", 30),
    ("6m", 182),
    ("1y", 365),
    ("all", None),
)
DEFAULT_RANGE = "all"

# Prefixed onto the range that is currently drawn. Telegram has no notion of a
# selected button, so the label has to say which one you are looking at.
ACTIVE_MARK = "· "


def _range_labels() -> dict[str, str]:
    """Translated at call time, so the table does not freeze the import locale."""
    return {
        "30d": _("30 days"),
        "6m": _("6 months"),
        "1y": _("1 year"),
        "all": _("All"),
    }


def _days(token: str) -> int | None:
    return dict(CHART_RANGES).get(token)


def _message_id(query: Any) -> int | None:
    return getattr(getattr(query, "message", None), "message_id", None)


def chart_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int | None,
    product_id: int,
    url: str,
    active: str,
) -> InlineKeyboardMarkup:
    """The ranges, Keepa's own chart where there is one, and the way out."""
    labels = _range_labels()
    buttons = [
        InlineKeyboardButton(
            (ACTIVE_MARK + labels[token]) if token == active else labels[token],
            callback_data=f"{CHART_RANGE_PREFIX}{product_id}|{token}",
        )
        for token, _window in CHART_RANGES
    ]
    rows = [buttons[:2], buttons[2:]]
    # Amazon only, and only when the URL actually carries an ASIN — the same
    # condition the product panel uses, since it is the same button.
    if extract_amazon_asin(url):
        rows.append(
            [InlineKeyboardButton(_("📈 Keepa graph"), callback_data=f"keepa_{product_id}")]
        )
    return result_keyboard(context, message_id, *rows)


async def _caption(db: Any, product_id: int, product: dict[str, Any]) -> str:
    """The name and the numbers the picture cannot state exactly.

    The image itself carries only "#id · shop"; the floor, the day it happened
    and what the price has averaged over each window belong in text, where they
    can be read precisely and copied.
    """
    from price_tracker.bot.handlers.product_list import (  # noqa: PLC0415 — cycle
        price_summary,
        summary_lines,
    )

    caption = f"📊 <b>#{product_id}</b> {_escape_html(product_label(product))}"
    stats = summary_lines(await price_summary(db, product), product.get("currency", "") or "EUR")
    if stats:
        caption += "\n" + "\n".join(stats)
    return caption


async def handle_chart_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Price history' button (`chart_<id>`).

    The chart replaces the panel it was opened from rather than piling up under
    it. Telegram cannot edit a text message into a photo, so the panel is deleted
    and the photo sent — and the navigation trail moves with it, which is what
    lets ◀️ Back reopen the panel afterwards.
    """
    if not data.startswith("chart_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "chart_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    origin_id = _message_id(query)
    chart = await generate_chart(db, product_id, product, days=_days(DEFAULT_RANGE))
    if not chart:
        await query.edit_message_text(
            _("📭 Not enough data to generate the chart (at least 2 points needed)."),
            reply_markup=result_keyboard(context, origin_id),
        )
        return True

    # Built against the panel's trail, which the photo is about to inherit.
    keyboard = chart_keyboard(context, origin_id, product_id, product.get("url", ""), DEFAULT_RANGE)
    with contextlib.suppress(TelegramError):
        await query.message.delete()
    photo = await query.message.reply_photo(
        photo=InputFile(chart, filename=f"chart_{product_id}.png"),
        caption=await _caption(db, product_id, product),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    if origin_id is not None:
        transfer_nav(context, origin_id, photo.message_id)
    return True


async def handle_chart_range(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Redraw an open chart over another window (`chartr|<id>|<range>`).

    Edited in place rather than resent: the message id is what the navigation
    trail is keyed by, so swapping the media keeps ◀️ Back pointing at the panel
    the chart was opened from however many ranges have been tried since.
    """
    if not data.startswith(CHART_RANGE_PREFIX):
        return False

    raw_id, _sep, token = data.removeprefix(CHART_RANGE_PREFIX).partition("|")
    if token not in dict(CHART_RANGES):
        return False

    # Resolved here rather than through `resolve_owned_product`, whose failure
    # path edits text: this callback only ever arrives on a photo, which
    # Telegram will not let anyone edit text into.
    product_id = _parse_id(raw_id)
    product = await _get_user_product(context, product_id, user_id) if product_id else None
    if product_id is None or not product:
        if product_id is not None:
            forget_product(context, product_id)
        with contextlib.suppress(TelegramError):
            await query.edit_message_caption(caption=_("❌ Product not found."))
        return True

    message_id = _message_id(query)
    url = product.get("url", "")
    caption = await _caption(db, product_id, product)
    chart = await generate_chart(db, product_id, product, days=_days(token))

    if not chart:
        # The picture stays as it is: it is still an honest drawing of the range
        # it was drawn for. Only the caption changes, to say why tapping did
        # nothing — replacing it with an apology would throw away the chart the
        # reader already had.
        with contextlib.suppress(TelegramError):
            await query.edit_message_caption(
                caption=caption + "\n\n" + _("📭 Nothing recorded in that range."),
                parse_mode=ParseMode.HTML,
                reply_markup=chart_keyboard(
                    context, message_id, product_id, url, _drawn_range(query)
                ),
            )
        return True

    await query.edit_message_media(
        media=InputMediaPhoto(
            media=InputFile(chart, filename=f"chart_{product_id}.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=chart_keyboard(context, message_id, product_id, url, token),
    )
    return True


def _drawn_range(query: Any) -> str:
    """Which range the photo on screen was actually drawn for.

    Read back off the message's own keyboard rather than remembered in
    `user_data`: the buttons already say which one is marked, and state that
    lives on the message cannot go stale or leak between messages.
    """
    markup = getattr(getattr(query, "message", None), "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", ()) or ():
        for button in row:
            data = getattr(button, "callback_data", "") or ""
            if data.startswith(CHART_RANGE_PREFIX) and str(button.text).startswith(ACTIVE_MARK):
                return data.rpartition("|")[2]
    return DEFAULT_RANGE
