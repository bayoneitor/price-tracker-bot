"""Product groups: named sets the user compares against each other.

Membership is many-to-many by design — a monitor belongs in "Monitors" and in
"Christmas gifts" at once, and forcing a single group would mean tracking the
same URL twice. Groups are addressed by an integer that travels in callback
data, so every read and write filters on the caller's user_id.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
OWNER = 1
INTRUDER = 2


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    # Cascade only fires with foreign keys on; the migration relies on it.
    await conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def _group(repo: Repository, name: str, *, user_id: int = OWNER) -> int:
    group_id = await repo.create_group(user_id=user_id, name=name)
    assert group_id is not None
    return group_id


async def _member_count(repo: Repository, group_id: int, *, user_id: int = OWNER) -> int:
    group = await repo.get_group(group_id, user_id=user_id)
    assert group is not None
    return group.member_count


async def _product(repo: Repository, name: str, *, user_id: int = OWNER) -> int:
    return await repo.add_product(
        user_id=user_id,
        url=f"https://example.com/p/{name}",
        name=name,
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )


async def test_a_group_holds_products_and_counts_them(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    for name in ("A", "B"):
        await repo.add_to_group(group_id, await _product(repo, name), user_id=OWNER)

    products = await repo.list_group_products(group_id, user_id=OWNER)
    assert [p.name for p in products] == ["A", "B"]
    assert await _member_count(repo, group_id) == 2


async def test_a_product_can_be_in_several_groups(repo: Repository) -> None:
    product_id = await _product(repo, "Monitor")
    for name in ("Monitors", "Christmas gifts"):
        await repo.add_to_group(await _group(repo, name), product_id, user_id=OWNER)

    groups = await repo.list_groups_for_product(product_id, user_id=OWNER)
    assert {g.name for g in groups} == {"Monitors", "Christmas gifts"}


async def test_adding_the_same_product_twice_is_harmless(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    product_id = await _product(repo, "A")

    assert await repo.add_to_group(group_id, product_id, user_id=OWNER) is True
    assert await repo.add_to_group(group_id, product_id, user_id=OWNER) is False
    assert len(await repo.list_group_products(group_id, user_id=OWNER)) == 1


async def test_two_groups_cannot_share_a_name(repo: Repository) -> None:
    assert await repo.create_group(user_id=OWNER, name="Monitors") is not None
    assert await repo.create_group(user_id=OWNER, name="Monitors") is None
    # …but a different user may use the same name.
    assert await repo.create_group(user_id=INTRUDER, name="Monitors") is not None


async def test_renaming_onto_an_existing_name_is_refused(repo: Repository) -> None:
    await _group(repo, "Monitors")
    other = await _group(repo, "Keyboards")

    assert await repo.rename_group(other, user_id=OWNER, name="Monitors") is False
    assert await repo.rename_group(other, user_id=OWNER, name="Mice") is True


async def test_deleting_a_product_removes_it_from_its_groups(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    keep = await _product(repo, "Keep")
    doomed = await _product(repo, "Doomed")
    await repo.add_to_group(group_id, keep, user_id=OWNER)
    await repo.add_to_group(group_id, doomed, user_id=OWNER)

    await repo.delete_product(doomed, user_id=OWNER)

    assert [p.name for p in await repo.list_group_products(group_id, user_id=OWNER)] == ["Keep"]


async def test_deleting_a_group_leaves_the_products_alone(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    product_id = await _product(repo, "A")
    await repo.add_to_group(group_id, product_id, user_id=OWNER)

    assert await repo.delete_group(group_id, user_id=OWNER) is True
    assert await repo.get_product(product_id) is not None


# ── Groups are addressed by a guessable integer ──────────────────────────


async def test_another_users_group_is_invisible(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    await repo.add_to_group(group_id, await _product(repo, "A"), user_id=OWNER)

    assert await repo.get_group(group_id, user_id=INTRUDER) is None
    assert await repo.list_group_products(group_id, user_id=INTRUDER) == []
    assert await repo.list_groups(user_id=INTRUDER) == []


async def test_another_users_group_cannot_be_changed(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    mine = await _product(repo, "Mine", user_id=INTRUDER)

    assert await repo.add_to_group(group_id, mine, user_id=INTRUDER) is False
    assert await repo.rename_group(group_id, user_id=INTRUDER, name="Stolen") is False
    assert await repo.delete_group(group_id, user_id=INTRUDER) is False
    survivor = await repo.get_group(group_id, user_id=OWNER)
    assert survivor is not None
    assert survivor.name == "Monitors"


async def test_a_foreign_product_cannot_be_added_to_my_group(repo: Repository) -> None:
    group_id = await _group(repo, "Monitors")
    theirs = await _product(repo, "Theirs", user_id=INTRUDER)

    assert await repo.add_to_group(group_id, theirs, user_id=OWNER) is False
    assert await repo.list_group_products(group_id, user_id=OWNER) == []


# ── The comparison reads every member's history in one query ─────────────


async def test_history_for_several_products_comes_back_keyed_by_product(
    repo: Repository,
) -> None:
    first = await _product(repo, "A")
    second = await _product(repo, "B")
    for price in ("100", "90"):
        await repo.add_price_history(first, Decimal(price))
    await repo.add_price_history(second, Decimal("50"))

    histories = await repo.get_price_history_for_products([first, second])

    assert [str(r.price) for r in histories[first]] == ["100", "90"]
    assert [str(r.price) for r in histories[second]] == ["50"]


async def test_one_chatty_product_cannot_crowd_out_the_others(repo: Repository) -> None:
    """A global LIMIT would return only the busiest product's rows."""
    chatty = await _product(repo, "Chatty")
    quiet = await _product(repo, "Quiet")
    for step in range(10):
        await repo.add_price_history(chatty, Decimal(100 + step))
    await repo.add_price_history(quiet, Decimal("50"))

    histories = await repo.get_price_history_for_products([chatty, quiet], limit_per_product=3)

    assert len(histories[chatty]) == 3
    assert len(histories[quiet]) == 1


async def test_asking_for_no_products_asks_the_database_nothing(repo: Repository) -> None:
    assert await repo.get_price_history_for_products([]) == {}


@pytest.mark.parametrize("missing", [999])
async def test_a_product_with_no_history_still_gets_a_key(repo: Repository, missing: int) -> None:
    assert await repo.get_price_history_for_products([missing]) == {missing: []}
