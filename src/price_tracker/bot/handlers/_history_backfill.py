"""Backfill a product's price history right after it is added.

Split out of `product.py` to keep that module under its 500-LOC budget, and
because this is the one piece of the add flow with real machinery behind it —
walking providers, filtering points, converting currency — worth testing on
its own with a fake registry and no network.

Every failure here is silent to the reader: a missed or broken backfill must
never look like a failed add. `_add_product` calls `backfill_history` once,
after the product exists, and only decorates the confirmation card if it
returns something.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode

from price_tracker.bot.charts import generate_chart
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.keyboards import close_button
from price_tracker.bot.labels import product_label
from price_tracker.core.currency import convert_to_eur

if TYPE_CHECKING:
    import httpx
    from telegram import Message

    from price_tracker.core.history_base import HistoryPoint
    from price_tracker.core.registry import HistoryRegistry

logger = logging.getLogger(__name__)

# A page render plus a network round trip, generously bounded: a provider that
# takes longer than this is a miss, not a reason to make the reader wait.
FETCH_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """What `backfill_history` found, for the confirmation card to describe."""

    count: int
    source: str
    first_observed_at: datetime
    lowest_price: Decimal
    average_price: Decimal


async def backfill_history(
    *,
    db: Any,
    client: httpx.AsyncClient,
    history_registry: HistoryRegistry | None,
    product_id: int,
    url: str,
    currency: str,
    added_at: datetime | None = None,
) -> BackfillOutcome | None:
    """Try every provider willing to handle `url`, in priority order.

    Stops at the first that returns usable points — a Keepa hit means
    PrecioReal is never asked — and writes them with `db.add_price_history_bulk`,
    which folds their low and high into the product's own extremes and records
    who backfilled it. Returns `None` when there is no registry, no provider
    matches, every provider misses, or the only points on offer are unusable
    (in the future, non-positive, or in a currency this deployment cannot
    convert) — the caller adds nothing to the confirmation card in that case,
    rather than reporting a backfill that did not actually happen.
    """
    if history_registry is None:
        return None
    cutoff = added_at or datetime.now(UTC)
    providers = history_registry.resolve_all(url)
    if not providers:
        logger.info("No history provider claimed %s", url[:80])
        return None

    for provider in providers:
        result = await _fetch_one(provider, url, client)
        if result is None or result.error is not None:
            if result is not None and result.error:
                logger.info(
                    "History provider %s missed %s: %s",
                    provider.name,
                    url[:80],
                    result.error,
                )
            continue
        points = _usable_points(result.points, before=cutoff)
        if not points:
            continue
        if result.currency:
            points = await _matched_currency(
                db, client, points, from_currency=result.currency, to_currency=currency
            )
            if not points:
                continue
        written = await db.add_price_history_bulk(product_id, points, source=provider.name)
        if written == 0:
            continue
        return BackfillOutcome(
            count=written,
            source=provider.name,
            first_observed_at=min(p.observed_at for p in points),
            lowest_price=min(p.price for p in points),
            average_price=_average_price(points),
        )
    return None


def _average_price(points: tuple[HistoryPoint, ...]) -> Decimal:
    total = sum((p.price for p in points), Decimal("0"))
    return (total / Decimal(len(points))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _fetch_one(provider: Any, url: str, client: httpx.AsyncClient) -> Any:
    """One provider's `fetch`, bounded and never raising past this point.

    `AbstractHistoryProvider.fetch` already promises not to raise — but that is
    a promise a plugin makes, not one this caller can enforce, and an operator
    plugin is edited outside review. A broken one must lose a backfill, not the
    add.
    """
    try:
        return await asyncio.wait_for(provider.fetch(url, client), timeout=FETCH_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.info("History provider %s timed out on %s", provider.name, url[:60])
        return None
    except Exception:  # noqa: BLE001 — a plugin's fetch must not abort the add
        logger.exception("History provider %s raised on %s", provider.name, url[:60])
        return None


def _usable_points(
    points: tuple[HistoryPoint, ...], *, before: datetime
) -> tuple[HistoryPoint, ...]:
    """Points from before tracking started, at a price that means something.

    `before` excludes anything at or after the moment the product was added —
    a provider's clock running fast, or its "today" entry landing after ours,
    would otherwise double up with the bot's own first read. `price <= 0` is
    every provider's way of marking "unavailable that day", not a price.
    """
    return tuple(p for p in points if p.price > 0 and p.observed_at < before)


async def _matched_currency(
    db: Any,
    client: httpx.AsyncClient,
    points: tuple[HistoryPoint, ...],
    *,
    from_currency: str,
    to_currency: str,
) -> tuple[HistoryPoint, ...]:
    """Points in the product's own tracked currency, or nothing.

    `convert_to_eur` is the only conversion this deployment has, because EUR is
    the only currency it has ever needed to convert *to* — every scraper here
    reads a European shop. A product tracked in something else is left with no
    conversion rather than a wrong one: dropping the whole batch is safer than
    mixing currencies into one chart's price axis.
    """
    if from_currency.upper() == to_currency.upper():
        return points
    if to_currency.upper() != "EUR":
        logger.info(
            "No conversion path %s -> %s; dropping %d imported points",
            from_currency,
            to_currency,
            len(points),
        )
        return ()
    converted = []
    for point in points:
        price = await convert_to_eur(db, point.price, from_currency, client)
        converted.append(dataclasses.replace(point, price=price))
    return tuple(converted)


async def send_backfill_chart(
    message: Message,
    db: Any,
    product_id: int,
    *,
    currency: str,
) -> None:
    """Attach the full imported+live chart to the confirmation. Never raises.

    A missing product, a sparse history, or matplotlib failing must not turn a
    successful add into an error the reader has to act on — the card already
    carried the numbers.
    """
    try:
        product = await db.get_product(product_id)
        if product is None:
            return
        chart_buf = await generate_chart(db, product_id, product)
        if chart_buf is None:
            return
        from price_tracker.bot.handlers.product_list import (  # noqa: PLC0415 — cycle
            price_summary,
            summary_lines,
        )

        caption = f"📊 <b>#{product_id}</b> {_escape_html(product_label(product))}"
        # The same lines the product screen and the price chart show, from the
        # same function: three captions describing one product differently is
        # how a reader stops trusting any of them.
        stats = summary_lines(await price_summary(db, product), currency)
        if stats:
            caption += "\n" + "\n".join(stats)
        await message.reply_photo(
            photo=InputFile(chart_buf, filename=f"chart_{product_id}.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[close_button()]]),
        )
    except Exception:  # noqa: BLE001 — the add already succeeded
        logger.exception("Could not send the backfill chart for product %s", product_id)
