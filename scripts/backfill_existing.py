"""One-off: backfill history for products already tracked, not just new ones.

`_add_product` calls `backfill_history` at add time; a product added before
this feature existed never got that pass. Same function, same filtering
(nothing at or after `added_at`, no non-positive prices, currency matched or
dropped) — the only difference is `added_at` comes from the product's own
`created_at` instead of "now", so nothing already checked is touched.

Run inside the container, where the plugins and their Chromium are:
    docker exec price-tracker-bot python scripts/backfill_existing.py [--dry-run]

Sends each product's owner a Telegram message with what was found — the same
card `_add_product` shows, plus the chart — so this is not a silent DB write.
Skips a product that already has `history_source` set, so running it twice is
harmless.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import aiosqlite
from telegram import Bot
from telegram.constants import ParseMode

from price_tracker.bot.decorators import _convert_display
from price_tracker.bot.handlers._helpers import _escape_html
from price_tracker.bot.handlers._history_backfill import backfill_history, send_backfill_chart
from price_tracker.bot.messages import _, reset_locale, set_locale
from price_tracker.config import Config
from price_tracker.core.http_client import build_client
from price_tracker.core.registry import HistoryRegistry, ScraperRegistry, discover_dropin_plugins
from price_tracker.db import apply_runtime_pragmas
from price_tracker.db.repository import Repository

PLUGIN_DIR = Path("/app/plugins")


def _parse_created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    config = Config.from_env()
    conn = await aiosqlite.connect(config.database_path)
    conn.row_factory = aiosqlite.Row
    await apply_runtime_pragmas(conn)
    repo = Repository(conn)

    scraper_registry = ScraperRegistry()
    history_registry = HistoryRegistry()
    discover_dropin_plugins(scraper_registry, history_registry, PLUGIN_DIR)
    print(f"History providers loaded: {[p.name for p in history_registry]}")

    bot = Bot(token=config.telegram_bot_token)
    client = build_client(timeout=float(config.request_timeout))
    total_hit = total_miss = 0

    async with client:
        for user in await repo.list_active_users():
            token = set_locale(await repo.get_user_language(user.user_id))
            try:
                for product in await repo.list_products_for_user(user_id=user.user_id):
                    if product.history_source:
                        print(f"#{product.id} {product.name!r}: already backfilled, skipping")
                        continue
                    if product.created_at is None:
                        print(f"#{product.id} {product.name!r}: no created_at, skipping")
                        continue

                    added_at = _parse_created_at(product.created_at)
                    print(f"#{product.id} {product.name!r} (tracking since {added_at.date()})...")
                    if dry_run:
                        continue

                    outcome = await backfill_history(
                        db=repo,
                        client=client,
                        history_registry=history_registry,
                        product_id=product.id,
                        url=product.url,
                        currency=product.currency or "EUR",
                        added_at=added_at,
                    )
                    if outcome is None:
                        print("  miss")
                        total_miss += 1
                        continue

                    total_hit += 1
                    print(
                        f"  hit: {outcome.count} points via {outcome.source}, "
                        f"low={outcome.lowest_price}, avg={outcome.average_price}"
                    )
                    currency = product.currency or "EUR"
                    text = _(
                        "📈 <b>#{pid}</b> {name}\n\n"
                        "Imported {count} historical prices since {date} (via {source}).\n"
                        "🏷 Lowest ever: <b>{low}</b> · Average: <b>{avg}</b>"
                    ).format(
                        pid=product.id,
                        name=_escape_html(product.name or product.url),
                        count=outcome.count,
                        date=outcome.first_observed_at.strftime("%Y-%m-%d"),
                        source=outcome.source,
                        low=_convert_display(outcome.lowest_price, currency),
                        avg=_convert_display(outcome.average_price, currency),
                    )
                    message = await bot.send_message(
                        chat_id=user.user_id, text=text, parse_mode=ParseMode.HTML
                    )
                    await send_backfill_chart(
                        message,
                        repo,
                        product.id,
                        currency=currency,
                        average_price=outcome.average_price,
                    )
            finally:
                reset_locale(token)

    print(f"\nDone. {total_hit} backfilled, {total_miss} missed.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
