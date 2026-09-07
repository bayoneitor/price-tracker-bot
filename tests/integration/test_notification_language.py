"""A notification is written in the language of the person receiving it.

Handlers resolve a locale from the update they are answering. The scheduler
answers no update: it composed every message under `deps.lang`, one language for
the whole deployment. A Spanish reader whose interactive replies were all in
Spanish still got "📉 Price drop!" at three in the morning.

Telegram reports `language_code` on every update. It is stored now (migration
018, written by `bot.decorators.restricted`) and read back here, hours later, by
a job with no update in front of it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


class _StubScraper(AbstractScraper):
    name = "stub"
    priority = 100

    def __init__(self, response: ProductInfo) -> None:
        self._response = response

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return self._response


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def _drop_for(repo: Repository, *, user_id: int) -> str:
    """Run one sweep that drops a price, returning the message that was sent."""
    await repo.add_product(
        user_id=user_id,
        url=f"https://example.com/p/{user_id}",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    registry = ScraperRegistry()
    registry.register(_StubScraper(ProductInfo(name="Widget", price=Decimal("50"), currency="EUR")))
    notifier = AsyncMock(return_value=True)
    async with httpx.AsyncClient() as client:
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=client,
                notifier=notifier,
                delay_between_products=0.0,
                lang="en",
            )
        )
        await scheduler.run_check_for_user(user_id=user_id)
    assert notifier.await_args is not None, "the drop crossed the threshold and must notify"
    return str(notifier.await_args.args[1])


# ── The language the reader speaks ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_price_drop_speaks_the_readers_language(repo: Repository) -> None:
    await repo.ensure_user(user_id=1)
    await repo.update_user_info(1, language_code="es-ES")

    assert "Bajada de precio" in await _drop_for(repo, user_id=1)


@pytest.mark.asyncio
async def test_the_deployment_language_is_the_fallback(repo: Repository) -> None:
    """A user who has not spoken since the column landed has no stored code."""
    await repo.ensure_user(user_id=2)

    assert "Price drop" in await _drop_for(repo, user_id=2)


@pytest.mark.asyncio
async def test_two_readers_in_one_sweep_get_their_own_language(repo: Repository) -> None:
    """The locale is per message, not per sweep — the bug a single set_locale hides."""
    await repo.ensure_user(user_id=1)
    await repo.update_user_info(1, language_code="es")
    await repo.ensure_user(user_id=2)
    await repo.update_user_info(2, language_code="it")

    assert "Bajada de precio" in await _drop_for(repo, user_id=1)
    assert "Prezzo in calo" in await _drop_for(repo, user_id=2)


@pytest.mark.asyncio
async def test_the_locale_does_not_outlive_the_message(repo: Repository) -> None:
    """Set and reset around each render: a leak would recolour whatever ran next."""
    from price_tracker.bot.messages import _, reset_locale, set_locale

    await repo.ensure_user(user_id=1)
    await repo.update_user_info(1, language_code="es")

    token = set_locale("en")
    try:
        await _drop_for(repo, user_id=1)
        assert _("❌ Product not found.") == "❌ Product not found."
    finally:
        reset_locale(token)


# ── Where the code comes from ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_language_is_remembered_across_updates(repo: Repository) -> None:
    await repo.ensure_user(user_id=1)
    await repo.update_user_info(1, display_name="Dave", language_code="es-ES")

    assert await repo.get_user_language(1) == "es-ES"


@pytest.mark.asyncio
async def test_an_update_carrying_no_language_keeps_the_stored_one(repo: Repository) -> None:
    """Telegram omits the field for some clients: absent is not 'reset to none'."""
    await repo.ensure_user(user_id=1)
    await repo.update_user_info(1, language_code="es-ES")
    await repo.update_user_info(1, display_name="Dave")

    assert await repo.get_user_language(1) == "es-ES"


@pytest.mark.asyncio
async def test_an_unknown_user_has_no_language(repo: Repository) -> None:
    assert await repo.get_user_language(999) is None
