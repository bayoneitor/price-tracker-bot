"""A product can be called what the user calls it.

Scraped titles are written for search engines: three of the same product across
three shops differ only somewhere past the fortieth character, and the index is
ten of those in a row. The alias replaces the displayed name everywhere at once,
because one function renders it, and the scraped name is kept so a rename never
hides what is actually being tracked.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot.handlers.product_list import build_product_view
from price_tracker.bot.labels import product_label
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
LISTED = 'LG UltraGear 27GP850-B 27" QHD 180Hz Nano IPS 1ms HDR400'


def _product(**extra: Any) -> dict[str, Any]:
    return {
        "id": 3,
        "name": LISTED,
        "url": "https://www.amazon.es/dp/X",
        "domain": "amazon.es",
        "current_price": "429.00",
        "currency": "EUR",
        "threshold_type": "percentage",
        "threshold_value": "10",
        **extra,
    }


# ── What gets shown ──────────────────────────────────────────────────────


def test_the_alias_replaces_the_name_wherever_it_is_rendered() -> None:
    assert product_label(_product(alias="Monitor salón")) == "Monitor salón · amazon.es"


def test_without_an_alias_the_shop_name_is_used() -> None:
    assert product_label(_product()) == f"{LISTED} · amazon.es"


def test_an_empty_alias_is_not_a_name() -> None:
    """A cleared alias is stored as NULL, but an empty string must not win either."""
    assert product_label(_product(alias="")) == f"{LISTED} · amazon.es"


def test_the_product_screen_still_shows_what_the_shop_calls_it() -> None:
    """A rename must not hide what is actually being tracked."""
    text, _markup = build_product_view(_product(alias="Monitor salón"))

    assert "Monitor salón" in text
    assert "Listed as" in text
    assert "LG UltraGear" in text


def test_without_an_alias_there_is_no_second_name_to_show() -> None:
    text, _markup = build_product_view(_product())

    assert "Listed as" not in text


# ── Storing it ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def _add(repo: Repository, user_id: int = 1) -> int:
    return await repo.add_product(
        user_id=user_id,
        url="https://www.amazon.es/dp/X",
        name=LISTED,
        domain="amazon.es",
        initial_price=Decimal("429"),
        currency="EUR",
    )


async def test_an_alias_is_stored_and_read_back(repo: Repository) -> None:
    pid = await _add(repo)

    assert await repo.set_alias(pid, "Monitor salón", user_id=1) is True

    product = await repo.get_product(pid)
    assert product is not None
    assert product.alias == "Monitor salón"
    assert product.name == LISTED, "the shop's name is kept"


async def test_clearing_the_alias_goes_back_to_the_shop_name(repo: Repository) -> None:
    pid = await _add(repo)
    await repo.set_alias(pid, "Monitor salón", user_id=1)

    await repo.set_alias(pid, None, user_id=1)

    product = await repo.get_product(pid)
    assert product is not None
    assert product.alias is None
    assert product_label(product) == f"{LISTED} · amazon.es"


async def test_another_users_product_cannot_be_renamed(repo: Repository) -> None:
    """The id travels in callback data, so the write filters on the owner."""
    pid = await _add(repo, user_id=1)

    assert await repo.set_alias(pid, "Stolen", user_id=2) is False

    product = await repo.get_product(pid)
    assert product is not None
    assert product.alias is None


# ── The prompt ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_naming_a_product_from_its_screen() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input
    from price_tracker.bot.navigation import PendingInput

    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.update_user_info = AsyncMock()
    db.get_product_for_user = AsyncMock(return_value=_product())
    context = MagicMock()
    context.user_data = {"pending_action": PendingInput("alias", 3)}
    context.bot_data = {"db": db}
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.text = "Monitor salón"
    update.message.reply_text = AsyncMock()

    await handle_text_input(update, context)

    db.set_alias.assert_awaited_once_with(3, "Monitor salón", user_id=1)


@pytest.mark.asyncio
async def test_a_dash_clears_the_name() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input
    from price_tracker.bot.navigation import PendingInput

    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.update_user_info = AsyncMock()
    db.get_product_for_user = AsyncMock(return_value=_product(alias="Monitor salón"))
    context = MagicMock()
    context.user_data = {"pending_action": PendingInput("alias", 3)}
    context.bot_data = {"db": db}
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.text = "-"
    update.message.reply_text = AsyncMock()

    await handle_text_input(update, context)

    db.set_alias.assert_awaited_once_with(3, None, user_id=1)
