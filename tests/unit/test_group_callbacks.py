"""The group buttons, including the ownership check every one of them needs.

Group ids travel in callback data as plain integers, so an unfiltered lookup
would let anyone open, rename or delete anyone's group by guessing a number —
the hole that had to be closed on the product buttons.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._groups import handle_group_buttons
from price_tracker.db.models import ProductGroup

OWNER = 1
INTRUDER = 2
GROUP_ID = 7


def _group(name: str = "Monitors", count: int = 2) -> ProductGroup:
    return ProductGroup(id=GROUP_ID, user_id=OWNER, name=name, member_count=count)


def _product(pid: int, price: str = "100") -> dict[str, Any]:
    return {"id": pid, "name": f"Widget {pid}", "current_price": price, "currency": "EUR"}


def _db(*, group: ProductGroup | None, products: list[dict[str, Any]] | None = None) -> AsyncMock:
    db = AsyncMock()
    db.get_group = AsyncMock(return_value=group)
    db.list_groups = AsyncMock(return_value=[group] if group else [])
    db.list_group_products = AsyncMock(return_value=products or [])
    db.get_active_products = AsyncMock(return_value=products or [])
    db.get_price_history_for_products = AsyncMock(return_value={})
    return db


def _query() -> MagicMock:
    return MagicMock(message=MagicMock(message_id=5), edit_message_text=AsyncMock())


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


def _data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback",
    [
        f"grp_open_{GROUP_ID}",
        f"grp_cmp_{GROUP_ID}",
        f"grp_lead_{GROUP_ID}",
        f"grp_ren_{GROUP_ID}",
        f"grp_del_{GROUP_ID}",
        f"grp_delok_{GROUP_ID}",
        f"grp_add_{GROUP_ID}",
        f"grp_rem_{GROUP_ID}",
    ],
)
async def test_another_users_group_is_never_touched(callback: str) -> None:
    db = _db(group=None)  # not visible to this caller
    query = _query()

    handled = await handle_group_buttons(query, _context(), db, INTRUDER, callback)

    assert handled is True
    db.delete_group.assert_not_awaited()
    db.rename_group.assert_not_awaited()
    assert "not found" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_opening_a_group_lists_its_products_and_offers_comparison() -> None:
    db = _db(group=_group(), products=[_product(1), _product(2)])
    query = _query()

    await handle_group_buttons(query, _context(), db, OWNER, f"grp_open_{GROUP_ID}")

    text = query.edit_message_text.await_args.args[0]
    assert "Monitors" in text
    buttons = _data(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert f"grp_cmp_{GROUP_ID}" in buttons
    assert f"grp_chart_{GROUP_ID}" in buttons


@pytest.mark.asyncio
async def test_a_group_with_one_product_offers_no_comparison() -> None:
    db = _db(group=_group(count=1), products=[_product(1)])
    query = _query()

    await handle_group_buttons(query, _context(), db, OWNER, f"grp_open_{GROUP_ID}")

    buttons = _data(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert f"grp_cmp_{GROUP_ID}" not in buttons
    assert f"grp_add_{GROUP_ID}" in buttons


@pytest.mark.asyncio
async def test_deleting_asks_first_and_says_the_products_survive() -> None:
    db = _db(group=_group(), products=[_product(1), _product(2)])
    query = _query()

    await handle_group_buttons(query, _context(), db, OWNER, f"grp_del_{GROUP_ID}")

    db.delete_group.assert_not_awaited()
    text = query.edit_message_text.await_args.args[0]
    assert "stay tracked" in text
    assert f"grp_delok_{GROUP_ID}" in _data(
        query.edit_message_text.await_args.kwargs["reply_markup"]
    )


@pytest.mark.asyncio
async def test_confirming_deletes_and_returns_to_the_list() -> None:
    db = _db(group=_group())
    query = _query()

    await handle_group_buttons(query, _context(), db, OWNER, f"grp_delok_{GROUP_ID}")

    db.delete_group.assert_awaited_once_with(GROUP_ID, user_id=OWNER)
    assert "Groups" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_the_add_picker_leaves_out_what_is_already_in_the_group() -> None:
    db = _db(group=_group(), products=[_product(1), _product(2)])
    db.list_group_products = AsyncMock(return_value=[_product(1)])
    db.get_active_products = AsyncMock(return_value=[_product(1), _product(2)])
    query = _query()

    await handle_group_buttons(query, _context(), db, OWNER, f"grp_add_{GROUP_ID}")

    buttons = _data(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert f"grp_put_{GROUP_ID}_2" in buttons
    assert f"grp_put_{GROUP_ID}_1" not in buttons


@pytest.mark.asyncio
async def test_adding_and_removing_go_through_the_owner_filtered_repository() -> None:
    db = _db(group=_group(), products=[_product(1)])
    context = _context()

    await handle_group_buttons(_query(), context, db, OWNER, f"grp_put_{GROUP_ID}_1")
    db.add_to_group.assert_awaited_once_with(GROUP_ID, 1, user_id=OWNER)

    await handle_group_buttons(_query(), context, db, OWNER, f"grp_pull_{GROUP_ID}_1")
    db.remove_from_group.assert_awaited_once_with(GROUP_ID, 1, user_id=OWNER)


@pytest.mark.asyncio
async def test_creating_a_group_asks_for_a_name_with_a_way_out() -> None:
    context = _context()
    query = _query()

    await handle_group_buttons(query, context, _db(group=None), OWNER, "grp_new")

    assert context.user_data["pending_action"].action == "group_new"
    assert "cancel_action" in _data(query.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_renaming_remembers_which_group_it_was_opened_for() -> None:
    context = _context()

    await handle_group_buttons(_query(), context, _db(group=_group()), OWNER, f"grp_ren_{GROUP_ID}")

    pending = context.user_data["pending_action"]
    assert (pending.action, pending.target_id) == ("group_rename", GROUP_ID)


@pytest.mark.asyncio
async def test_a_chart_with_too_little_history_says_so_instead_of_failing() -> None:
    db = _db(group=_group(), products=[_product(1), _product(2)])
    query = _query()

    await handle_group_buttons(query, _context(), db, OWNER, f"grp_chart_{GROUP_ID}")

    query.message.reply_photo.assert_not_called()
    assert "Not enough history" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unrelated_callbacks_fall_through() -> None:
    assert not await handle_group_buttons(_query(), _context(), _db(group=None), OWNER, "edit_3")
