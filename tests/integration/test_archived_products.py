"""Deleting a product stops tracking it and hides it. It does not destroy it.

`DELETE FROM products` cascaded to `price_history`, so removing a product from
the listing threw away every reading ever taken of it — the one part of a
tracker that cannot be recreated. Re-adding the link started from zero and the
months in between were simply gone.

A deletion is an archive now: the row stops being checked, stops appearing
anywhere in the interface, and keeps its history, which comes back with it if
the same URL is added again.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest_asyncio

from price_tracker.db import apply_runtime_pragmas
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
URL = "https://www.mediamarkt.es/es/product/_x-1.html"


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    await apply_runtime_pragmas(conn)
    r = Repository(conn)
    await r.ensure_user(user_id=1)
    await r.ensure_user(user_id=2)
    try:
        yield r
    finally:
        await conn.close()


async def _tracked(repo: Repository, *, user_id: int = 1, url: str = URL) -> int:
    pid = await repo.add_product(
        user_id=user_id,
        url=url,
        name="Widget",
        domain="mediamarkt.es",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    await repo.add_price_history(pid, Decimal("95"))
    await repo.add_price_history(pid, Decimal("90"))
    return pid


# ── What survives ────────────────────────────────────────────────────────


async def test_the_price_history_survives_a_deletion(repo: Repository) -> None:
    pid = await _tracked(repo)

    await repo.delete_product(pid, user_id=1)

    assert len(await repo.get_price_history(pid, limit=10)) == 2


async def test_the_product_is_gone_from_every_listing(repo: Repository) -> None:
    pid = await _tracked(repo)

    await repo.delete_product(pid, user_id=1)

    assert await repo.get_active_products(1) == []
    # `get_all_products` is what counts the "N paused" button: an archived
    # product must not resurface there as though it were merely paused.
    assert await repo.get_all_products(1) == []
    assert await repo.list_products_for_user(user_id=1) == []


async def test_the_product_cannot_be_opened_by_id(repo: Repository) -> None:
    """Typing `#3` for something deleted must not reach it."""
    pid = await _tracked(repo)

    await repo.delete_product(pid, user_id=1)

    assert await repo.get_product_for_user(pid, 1) is None
    assert await repo.get_product(pid) is None


async def test_it_does_not_count_as_one_of_your_products(repo: Repository) -> None:
    pid = await _tracked(repo)

    await repo.delete_product(pid, user_id=1)
    stats = await repo.get_stats(user_id=1)

    assert stats["total_products"] == 0
    assert stats["active_products"] == 0


async def test_a_group_does_not_list_a_deleted_member(repo: Repository) -> None:
    pid = await _tracked(repo)
    gid = await repo.create_group(user_id=1, name="Monitors")
    assert gid is not None
    await repo.add_to_group(gid, pid, user_id=1)

    await repo.delete_product(pid, user_id=1)

    assert await repo.list_group_products(gid, user_id=1) == []


async def test_a_deleted_product_is_not_swept(repo: Repository) -> None:
    """What "stop tracking it" means: the sweep reads this list."""
    pid = await _tracked(repo)

    await repo.delete_product(pid, user_id=1)

    assert await repo.list_products_for_user(user_id=1, only_active=True) == []


# ── Coming back ──────────────────────────────────────────────────────────


async def test_adding_the_link_again_brings_the_product_back(repo: Repository) -> None:
    pid = await _tracked(repo)
    await repo.delete_product(pid, user_id=1)

    restored = await repo.restore_archived_product(URL, 1)

    assert restored is not None
    assert restored.id == pid


async def test_it_comes_back_with_its_history(repo: Repository) -> None:
    """The point of keeping it: not a new product pointing at the same page."""
    pid = await _tracked(repo)
    await repo.delete_product(pid, user_id=1)

    await repo.restore_archived_product(URL, 1)

    assert len(await repo.get_price_history(pid, limit=10)) == 2
    assert [p.id for p in await repo.get_active_products(1)] == [pid]


async def test_a_url_that_was_never_deleted_restores_nothing(repo: Repository) -> None:
    await _tracked(repo)

    assert await repo.restore_archived_product(URL, 1) is None


async def test_another_users_archived_product_is_not_restored(repo: Repository) -> None:
    pid = await _tracked(repo, user_id=2)
    await repo.delete_product(pid, user_id=2)

    assert await repo.restore_archived_product(URL, 1) is None


# ── The boundaries ───────────────────────────────────────────────────────


async def test_deleting_someone_elses_product_does_nothing(repo: Repository) -> None:
    pid = await _tracked(repo, user_id=2)

    assert await repo.delete_product(pid, user_id=1) is False
    assert [p.id for p in await repo.get_active_products(2)] == [pid]


async def test_deleting_twice_reports_the_second_as_a_no_op(repo: Repository) -> None:
    """A stale button must not move the timestamp of a deletion already made."""
    pid = await _tracked(repo)

    assert await repo.delete_product(pid, user_id=1) is True
    assert await repo.delete_product(pid, user_id=1) is False


async def test_one_users_deletion_leaves_another_user_tracking(repo: Repository) -> None:
    """Rows are per user, so a shared URL is two products checked independently."""
    mine = await _tracked(repo, user_id=1)
    theirs = await _tracked(repo, user_id=2)

    await repo.delete_product(mine, user_id=1)

    assert [p.id for p in await repo.list_products_for_user(user_id=2, only_active=True)] == [
        theirs
    ]
