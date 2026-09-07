"""End-to-end checks the plan's own verification step asks for:

- adding a product with an empty `plugins/` is unchanged — the exact same
  confirmation card it always produced, no fact block, no extra buttons;
- a fake plugin discovered from a real file on disk, through the real
  registry, actually backfills a real database row with `source` set, and a
  higher-priority provider's hit stops a lower-priority one from being asked.

Everything else about the seam already has its own focused tests
(`test_history_registry.py`, `test_history_backfill.py`,
`test_history_backfill_repository.py`, `test_registry.py`); this file is only
about the chain between them holding together.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot.handlers.product import _add_product
from price_tracker.core.registry import HistoryRegistry, ScraperRegistry, discover_dropin_plugins
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
URL = "https://www.example.com/p/1"


class _StubScraper(AbstractScraper):
    name = "stub"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="Widget", price=Decimal("50"), currency="EUR")


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    r = Repository(conn)
    await r.ensure_user(user_id=1)
    try:
        yield r
    finally:
        await conn.close()


def _update_and_context(
    repo: Repository, *, history_registry: HistoryRegistry | None
) -> tuple[Any, Any, Any]:
    scraper = ScraperRegistry()
    scraper.register(_StubScraper())

    msg = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock(return_value=msg)

    context = MagicMock()
    context.bot_data = {
        "db": repo,
        "http_client": MagicMock(),
        "scraper": scraper,
        "history_registry": history_registry,
    }
    return update, context, msg


# ── Unchanged when there is nothing to backfill from ─────────────────────


@pytest.mark.asyncio
async def test_adding_a_product_with_no_registry_is_unchanged(repo: Repository) -> None:
    """No `history_registry` published at all — the state before this plan."""
    update, context, msg = _update_and_context(repo, history_registry=None)

    await _add_product(update, context, URL)

    card = msg.edit_text.await_args.args[0]
    assert "Imported" not in card
    keyboard = msg.edit_text.await_args.kwargs["reply_markup"]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]
    assert not any(d.startswith("bfmin_target_") for d in data)


@pytest.mark.asyncio
async def test_adding_a_product_with_an_empty_registry_is_unchanged(repo: Repository) -> None:
    """A `history_registry` exists but nothing is registered in it — `plugins/`
    with no history-provider files, which is the default on a fresh install."""
    update, context, msg = _update_and_context(repo, history_registry=HistoryRegistry())

    await _add_product(update, context, URL)

    card = msg.edit_text.await_args.args[0]
    assert "Imported" not in card
    product = await repo.get_product_by_url_for_user(URL, 1)
    assert product is not None
    assert product.history_source is None


# ── A fake plugin, discovered from a real file, through the real registry ─


_KEEPA_LIKE = """
from datetime import UTC, datetime
from decimal import Decimal

from price_tracker.core.history_base import AbstractHistoryProvider, HistoryPoint, HistoryResult


class FakeKeepa(AbstractHistoryProvider):
    name = "fakekeepa"
    priority = 100

    def can_handle(self, url):
        return True

    async def fetch(self, url, client):
        return HistoryResult(
            points=(
                HistoryPoint(observed_at=datetime(2020, 1, 1, tzinfo=UTC), price=Decimal("40")),
                HistoryPoint(observed_at=datetime(2020, 6, 1, tzinfo=UTC), price=Decimal("35")),
            ),
            currency="EUR",
        )
"""

_LOWER_PRIORITY_MISS = """
from price_tracker.core.history_base import AbstractHistoryProvider, HistoryResult


class FakeLowPriority(AbstractHistoryProvider):
    name = "fakelow"
    priority = 10

    def can_handle(self, url):
        return True

    async def fetch(self, url, client):
        raise AssertionError("must not be asked — the higher-priority provider already hit")
"""


@pytest.mark.asyncio
async def test_a_discovered_plugin_actually_backfills_the_database(
    repo: Repository, tmp_path: Path
) -> None:
    (tmp_path / "fakekeepa.py").write_text(_KEEPA_LIKE)
    (tmp_path / "fakelow.py").write_text(_LOWER_PRIORITY_MISS)

    scraper_registry = ScraperRegistry()
    history_registry = HistoryRegistry()
    discover_dropin_plugins(scraper_registry, history_registry, tmp_path)
    assert {p.name for p in history_registry} == {"fakekeepa", "fakelow"}

    update, context, msg = _update_and_context(repo, history_registry=history_registry)

    await _add_product(update, context, URL)

    card = msg.edit_text.await_args.args[0]
    assert "Imported 2 historical prices" in card
    assert "fakekeepa" in card
    assert "Lowest ever" in card
    assert "Average" in card
    msg.reply_photo.assert_awaited()

    product = await repo.get_product_by_url_for_user(URL, 1)
    assert product is not None
    assert product.history_source == "fakekeepa"
    assert product.lowest_price == Decimal("35")

    history = await repo.get_price_history(product.id, limit=10)
    imported = [h for h in history if h.source == "fakekeepa"]
    assert len(imported) == 2
