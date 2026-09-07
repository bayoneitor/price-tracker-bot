"""The 'Keepa graph' button (`keepa_<id>`).

Split out of `_product.py` to keep that module under its 500-LOC budget —
this handler earned a spot beside it, not inside it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from telegram.constants import ParseMode
from telegram.error import TelegramError

from price_tracker.bot.handlers._helpers import _escape_html, resolve_owned_product
from price_tracker.bot.keyboards import result_keyboard
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import transfer_nav
from price_tracker.core.url_utils import extract_amazon_asin, keepa_domain_code

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


def _message_id(query: Any) -> int | None:
    return getattr(getattr(query, "message", None), "message_id", None)


async def handle_keepa_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Keepa graph' button (`keepa_<id>`).

    Sends Keepa's own free PNG — a chart this bot never draws and a service it
    never talks to, so there is no plugin behind this: an image URL Telegram
    fetches itself, credited in the caption. Amazon only, since Keepa only
    knows Amazon.
    """
    if not data.startswith("keepa_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "keepa_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    url = product.get("url", "")
    asin = extract_amazon_asin(url)
    code = keepa_domain_code(url)
    if asin is None or code is None:
        await query.edit_message_text(
            _("❌ No Amazon ASIN found for this product."),
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return True

    origin_id = _message_id(query)
    graph_url = f"https://graph.keepa.com/pricehistory.png?asin={asin}&domain={code}&range=365"
    caption = _("📈 <b>#{pid}</b> {name}\n\nGraph by Keepa (keepa.com), 365 days.").format(
        pid=product_id, name=_escape_html(product_label(product))
    )

    keyboard = result_keyboard(context, origin_id)
    with contextlib.suppress(TelegramError):
        await query.message.delete()
    photo = await query.message.reply_photo(
        photo=graph_url,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    if origin_id is not None:
        transfer_nav(context, origin_id, photo.message_id)
    return True
