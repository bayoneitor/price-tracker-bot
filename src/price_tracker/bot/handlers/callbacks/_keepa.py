"""The 'Keepa graph' button (`keepa_<id>`).

Split out of `_product.py` to keep that module under its 500-LOC budget —
this handler earned a spot beside it, not inside it.
"""

from __future__ import annotations

import contextlib
import io
import logging
from typing import TYPE_CHECKING, Any

import httpx
from telegram import InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError

from price_tracker.bot.decorators import _client
from price_tracker.bot.handlers._helpers import _escape_html, resolve_owned_product
from price_tracker.bot.keyboards import result_keyboard
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import transfer_nav
from price_tracker.core.url_utils import extract_amazon_asin, keepa_domain_code

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


logger = logging.getLogger(__name__)


def _message_id(query: Any) -> int | None:
    return getattr(getattr(query, "message", None), "message_id", None)


async def handle_keepa_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Keepa graph' button (`keepa_<id>`).

    Sends Keepa's own free PNG — a chart this bot never draws, credited in the
    caption. Amazon only, since Keepa only knows Amazon. No plugin behind it:
    one image fetched over plain HTTP, no browser, which is why this keeps
    working while the Keepa *history* provider does not (that one needs the
    site's own SPA, which sits behind an anti-bot challenge).
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

    # Fetched here rather than handed to Telegram as a URL. Keepa serves this
    # endpoint by User-Agent: a browser one gets the PNG, anything else — which
    # is what Telegram's own fetcher looks like — gets 403 and an HTML error
    # body. Downloading it ourselves, with the same headers every scraper here
    # already sends, is also what lets a failure be reported *before* the panel
    # is deleted, instead of losing the panel to an unhandled BadRequest.
    image = await _fetch_graph(context, graph_url)
    if image is None:
        await query.edit_message_text(
            _("📉 Keepa has no graph for this product right now."),
            reply_markup=result_keyboard(context, origin_id),
        )
        return True

    caption = _("📈 <b>#{pid}</b> {name}\n\nGraph by Keepa (keepa.com), 365 days.").format(
        pid=product_id, name=_escape_html(product_label(product))
    )

    keyboard = result_keyboard(context, origin_id)
    with contextlib.suppress(TelegramError):
        await query.message.delete()
    photo = await query.message.reply_photo(
        photo=InputFile(image, filename=f"keepa_{product_id}.png"),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    if origin_id is not None:
        transfer_nav(context, origin_id, photo.message_id)
    return True


async def _fetch_graph(context: ContextTypes.DEFAULT_TYPE, graph_url: str) -> io.BytesIO | None:
    """Keepa's PNG as bytes, or None when it will not serve one.

    None covers every way this can fail — a 403, a network error, an HTML
    error page served with a 200 — so the caller has one branch to handle and
    never sends Telegram something that is not an image.
    """
    from price_tracker.core.scraper_base import get_headers  # noqa: PLC0415 — import cycle

    try:
        client = _client(context)
        response = await client.get(graph_url, headers=get_headers(), follow_redirects=True)
    except (httpx.HTTPError, KeyError) as exc:
        logger.info("Keepa graph fetch failed: %s", exc)
        return None
    if response.status_code != 200 or not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
        logger.info(
            "Keepa served no graph (status %s, %s)",
            response.status_code,
            response.headers.get("content-type"),
        )
        return None
    return io.BytesIO(response.content)
