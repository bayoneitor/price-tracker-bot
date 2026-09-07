"""Choosing a product is a short screen and one tap.

The pickers behind `/check`, `/pause`, `/target`, `/threshold`, `/refresh` and
`/history` put the product's whole name inside a button, and listed every active
product with no page. A shop's own title wraps to three lines, so twenty-five
products meant seventy-five lines of keyboard — the wall the product index had
just been rebuilt to avoid, on the screens that rebuild did not touch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._picker import handle_picker_page, resolve_picker
from price_tracker.bot.handlers.product_list import PAGE_SIZE, build_picker_view
from price_tracker.bot.keyboards import CLOSE_CALLBACK, PICKER_PREFIX
from price_tracker.db.models import ProductGroup

LISTED = 'LG UltraGear 27GP850-B 27" QHD 180Hz Nano IPS 1ms HDR400 G-Sync'


def _product(pid: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": pid,
        "name": LISTED,
        "domain": "amazon.es",
        "url": f"https://amazon.es/p/{pid}",
        "current_price": "429.00",
        "currency": "EUR",
        **extra,
    }


def _data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _texts(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _view(count: int, page: int = 0, prefix: str = "check_") -> tuple[str, Any]:
    return build_picker_view(
        [_product(i) for i in range(1, count + 1)], page, prefix=prefix, title="Pick one"
    )


# ── The screen ───────────────────────────────────────────────────────────


def test_a_page_holds_ten_and_the_rest_wait() -> None:
    _text, markup = _view(25)

    assert len([d for d in _data(markup) if d.startswith("check_")]) == PAGE_SIZE


def test_a_button_is_the_action_itself() -> None:
    """The contract every existing action handler already depends on."""
    _text, markup = _view(3)

    assert [d for d in _data(markup) if d.startswith("check_")] == [
        "check_1",
        "check_2",
        "check_3",
    ]


def test_a_button_carries_the_id_the_shop_and_the_price() -> None:
    _text, markup = _view(1)

    label = next(t for t in _texts(markup) if t.startswith("#1"))
    assert "amazon.es" in label
    assert "€429.00" in label


def test_the_name_is_cut_so_a_button_stays_one_line() -> None:
    """It wraps rather than truncating, and three lines per product is the wall."""
    _text, markup = _view(1)

    label = next(t for t in _texts(markup) if t.startswith("#1"))
    assert LISTED not in label
    assert "…" in label


def test_no_page_row_for_a_single_page() -> None:
    _text, markup = _view(4)

    assert not [d for d in _data(markup) if d.startswith(PICKER_PREFIX)]


def test_the_page_row_wraps_so_it_keeps_its_shape() -> None:
    """Three buttons at both ends: an arrow that vanishes moves the rest."""
    _text, first = _view(25, 0)
    _text, last = _view(25, 2)

    p = PICKER_PREFIX
    assert _data(first)[-4:-1] == [f"{p}check_|2", f"{p}check_|0", f"{p}check_|1"]
    assert _data(last)[-4:-1] == [f"{p}check_|1", f"{p}check_|2", f"{p}check_|0"]
    assert _texts(first)[-3] == "Page 1/3"


def test_a_page_past_the_end_is_clamped() -> None:
    _text, markup = _view(3, 99)

    assert "check_1" in _data(markup)


def test_an_empty_list_still_offers_a_way_out() -> None:
    text, markup = build_picker_view([], 0, prefix="check_", title="Pick one")

    assert "no tracked products" in text
    assert CLOSE_CALLBACK in _data(markup)


# ── Turning the page ─────────────────────────────────────────────────────


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


@pytest.mark.asyncio
async def test_turning_the_page_keeps_the_action() -> None:
    """The page token carries the prefix, so page two still checks."""
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(i) for i in range(1, 26)]))
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    handled = await handle_picker_page(query, _context(), db, 7, f"{PICKER_PREFIX}check_|1")

    assert handled is True
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "check_11" in _data(markup)
    assert "check_1" not in _data(markup)


@pytest.mark.asyncio
async def test_the_list_is_rebuilt_not_remembered() -> None:
    """A picker opened ten minutes ago must not offer a product deleted since."""
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1)]))
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    await handle_picker_page(query, _context(), db, 7, f"{PICKER_PREFIX}check_|0")

    db.get_active_products.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_an_unknown_prefix_says_so_rather_than_drawing_nothing() -> None:
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    handled = await handle_picker_page(query, _context(), AsyncMock(), 7, f"{PICKER_PREFIX}zzz_|0")

    assert handled is True
    assert "no longer available" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unrelated_callbacks_fall_through() -> None:
    assert not await handle_picker_page(MagicMock(), _context(), AsyncMock(), 7, "check_3")


# ── The group pickers, which filter ──────────────────────────────────────


def _group_db(members: list[int], all_ids: list[int]) -> AsyncMock:
    db = AsyncMock()
    db.get_group = AsyncMock(return_value=ProductGroup(id=7, user_id=1, name="Monitors"))
    db.list_group_products = AsyncMock(return_value=[_product(i) for i in members])
    db.get_active_products = AsyncMock(return_value=[_product(i) for i in all_ids])
    return db


@pytest.mark.asyncio
async def test_adding_offers_only_what_is_not_in_the_group() -> None:
    db = _group_db(members=[1, 2], all_ids=list(range(1, 26)))

    resolved = await resolve_picker(db, 1, "grp_put_7_")

    assert resolved is not None
    title, products = resolved
    assert "Monitors" in title
    assert [p["id"] for p in products][:3] == [3, 4, 5]


@pytest.mark.asyncio
async def test_the_filter_survives_a_page_turn() -> None:
    """The second page is rebuilt from the same rule, not from a stored slice."""
    db = _group_db(members=[1, 2], all_ids=list(range(1, 26)))
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    await handle_picker_page(query, _context(), db, 1, f"{PICKER_PREFIX}grp_put_7_|1")

    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "grp_put_7_1" not in _data(markup)
    assert "grp_put_7_2" not in _data(markup)


@pytest.mark.asyncio
async def test_removing_offers_only_the_members() -> None:
    db = _group_db(members=[4, 5], all_ids=list(range(1, 26)))

    resolved = await resolve_picker(db, 1, "grp_pull_7_")

    assert resolved is not None
    title, products = resolved
    assert [p["id"] for p in products] == [4, 5]
    assert "Monitors" in title


@pytest.mark.asyncio
async def test_another_users_group_resolves_to_nothing() -> None:
    db = _group_db(members=[], all_ids=[])
    db.get_group = AsyncMock(return_value=None)

    assert await resolve_picker(db, 2, "grp_put_7_") is None
