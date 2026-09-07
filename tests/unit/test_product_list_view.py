"""`/list`: an index of products, and one screen per product.

It began as one message per product, which buried the chat. The reply to that was
a single message holding a text index, one selected product's card and its action
buttons — but the buttons acted on whichever product the index had marked with a
`▸`, so seven rows of buttons never said what any of them would touch.

Two screens now. The index is a written list — ten to a page, each with its shop
and its price, the one under the cursor marked — and its buttons only move: one
row steps the cursor, another jumps a page, a third opens what the cursor is on.
The product's own screen is where the actions live, under the name they apply to.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton
from telegram.error import BadRequest

from price_tracker.bot.handlers.callbacks._list import handle_list_navigation
from price_tracker.bot.handlers.callbacks._nav import handle_close
from price_tracker.bot.handlers.product_list import (
    INDEX_PAGE_SIZE,
    build_index_view,
    build_product_view,
    cmd_list,
    page_count,
)
from price_tracker.bot.keyboards import (
    BACK_CALLBACK,
    CLOSE_CALLBACK,
    LIST_GOTO_PREFIX,
    PRODUCT_PREFIX,
)
from price_tracker.bot.navigation import NAV_KEY, PendingInput


def _product(pid: int, name: str = "Widget") -> dict[str, Any]:
    return {
        "id": pid,
        "name": f"{name} {pid}",
        "url": f"https://www.mediamarkt.es/es/product/_x-{pid}.html",
        "domain": "mediamarkt.es",
        "current_price": "259.00",
        "initial_price": "349.00",
        "currency": "EUR",
        "threshold_type": "percentage",
        "threshold_value": "10",
        "is_active": 1,
    }


def _button_data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _button_text(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# ── The index ────────────────────────────────────────────────────────────


def test_the_page_is_written_out_with_shops_and_prices() -> None:
    text, _markup = build_index_view([_product(i) for i in range(1, 4)], 0)

    for pid in (1, 2, 3):
        assert f"#{pid}</b> Widget {pid} · mediamarkt.es — €259.00" in text


def test_the_entries_are_separated_by_a_blank_line() -> None:
    """A name long enough to wrap runs into the next product otherwise, and
    every line starts with a number, so nothing else tells them apart."""
    text, _markup = build_index_view([_product(i) for i in range(1, 4)], 0)

    body = text.split("\n\n", 1)[1]
    assert body.count("\n\n") == 2
    assert "\n<b>#" not in body.replace("\n\n<b>#", "")


def test_every_product_on_the_page_is_a_button_of_its_own_number() -> None:
    """The number on the button is the number on the line, and the product id."""
    _text, markup = build_index_view([_product(i) for i in range(1, 4)], 0)

    assert [b.text for b in markup.inline_keyboard[0]] == ["#1", "#2", "#3"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        f"{PRODUCT_PREFIX}1",
        f"{PRODUCT_PREFIX}2",
        f"{PRODUCT_PREFIX}3",
    ]


def test_the_numbers_are_three_to_a_row() -> None:
    """Wider and Telegram shrinks the labels, narrower and the grid is a column."""
    _text, markup = build_index_view([_product(i) for i in range(1, 10)], 0)

    grid = markup.inline_keyboard[:2]
    assert [len(row) for row in grid] == [3, 3]


def test_a_part_filled_grid_does_not_pad_itself() -> None:
    _text, markup = build_index_view([_product(i) for i in range(1, 6)], 0)

    assert [len(row) for row in markup.inline_keyboard[:2]] == [3, 2]


def test_a_page_holds_six_products_and_the_rest_wait() -> None:
    """Nine fit a tidier grid, but the written list alone ran past a phone's
    screen before the grid and the exit row even entered the chat."""
    text, markup = build_index_view([_product(i) for i in range(1, 26)], 0)

    assert "#6</b>" in text
    assert "#7</b>" not in text
    assert len([d for d in _button_data(markup) if d.startswith(PRODUCT_PREFIX)]) == 6


def test_the_page_follows_the_position() -> None:
    """One piece of state travels, so page and contents cannot disagree."""
    text, _markup = build_index_view([_product(i) for i in range(1, 26)], 14)

    assert "#13</b>" in text
    assert "#18</b>" in text
    assert "#19</b>" not in text


@pytest.mark.parametrize(("total", "pages"), [(0, 1), (1, 1), (10, 1), (11, 2), (21, 3)])
def test_pages_are_counted_from_the_products(total: int, pages: int) -> None:
    assert page_count(total) == pages


@pytest.mark.parametrize(("total", "pages"), [(0, 1), (6, 1), (7, 2), (12, 2), (13, 3)])
def test_the_index_counts_its_own_pages_in_sixes(total: int, pages: int) -> None:
    assert page_count(total, INDEX_PAGE_SIZE) == pages


def test_the_grid_and_the_pages_are_separate_rows() -> None:
    """Opening a product and turning a page must not read as one control."""
    products = [_product(i) for i in range(1, 19)]  # 3 pages of 6

    _text, markup = build_index_view(products, 0)

    assert [b.callback_data for b in markup.inline_keyboard[1]] == [
        f"{PRODUCT_PREFIX}4",
        f"{PRODUCT_PREFIX}5",
        f"{PRODUCT_PREFIX}6",
    ]
    assert [b.callback_data for b in markup.inline_keyboard[2]] == [
        f"{LIST_GOTO_PREFIX}2",
        f"{LIST_GOTO_PREFIX}0",
        f"{LIST_GOTO_PREFIX}1",
    ]


def test_the_page_row_keeps_its_shape_at_both_ends() -> None:
    """An arrow that vanishes moves every button beside it."""
    products = [_product(i) for i in range(1, 19)]  # 3 pages of 6

    _t, first = build_index_view(products, 0)
    _t, last = build_index_view(products, 12)

    assert len(first.inline_keyboard[2]) == len(last.inline_keyboard[2]) == 3
    assert first.inline_keyboard[2][0].callback_data == f"{LIST_GOTO_PREFIX}2"
    assert last.inline_keyboard[2][2].callback_data == f"{LIST_GOTO_PREFIX}0"


def test_the_way_out_is_one_row_of_three() -> None:
    """Back where there is one, add, close — the row that ends every page."""
    context = MagicMock()
    context.user_data = {NAV_KEY: {7: ["menu_main", f"{LIST_GOTO_PREFIX}0"]}}

    _text, markup = build_index_view([_product(1)], 0, context=context, message_id=7)

    assert [b.callback_data for b in markup.inline_keyboard[-1]] == [
        BACK_CALLBACK,
        "menu_add",
        CLOSE_CALLBACK,
    ]


def test_a_listing_with_nowhere_to_go_back_to_offers_two() -> None:
    """/list opened from nothing: a Back button that leads nowhere is worse."""
    _text, markup = build_index_view([_product(1)], 0)

    assert [b.callback_data for b in markup.inline_keyboard[-1]] == ["menu_add", CLOSE_CALLBACK]


def test_a_single_page_has_no_page_row() -> None:
    _text, markup = build_index_view([_product(1), _product(2)], 0)

    assert not [d for d in _button_data(markup) if d.startswith(LIST_GOTO_PREFIX)]


def test_a_position_past_the_end_is_clamped_not_raised() -> None:
    """A stale button from an index whose products have since been deleted."""
    text, _markup = build_index_view([_product(1)], 99)

    assert "#1</b>" in text


def test_an_empty_list_still_offers_a_way_out() -> None:
    text, markup = build_index_view([], 0)

    assert "no tracked products" in text
    assert CLOSE_CALLBACK in _button_data(markup)


def test_extra_rows_sit_above_the_way_out() -> None:
    rows = [[InlineKeyboardButton("extra", callback_data="extra")]]

    _text, markup = build_index_view([_product(1)], 0, extra_rows=rows)

    data = _button_data(markup)
    assert data.index("extra") < data.index(CLOSE_CALLBACK)


# ── One product's screen ─────────────────────────────────────────────────


def test_the_product_screen_names_what_the_buttons_will_touch() -> None:
    text, markup = build_product_view(_product(3))

    assert "<b>#3</b> Widget 3 · mediamarkt.es" in text
    for action in ("check_3", "chart_3", "pause_3", "remove_3", "edit_3"):
        assert action in _button_data(markup)


def test_an_amazon_product_offers_the_keepa_button() -> None:
    amazon = {**_product(3), "url": "https://www.amazon.es/dp/B0CX23V2ZK"}

    _text, markup = build_product_view(amazon)

    assert "keepa_3" in _button_data(markup)


def test_a_non_amazon_product_offers_no_keepa_button() -> None:
    _text, markup = build_product_view(_product(3))

    assert "keepa_3" not in _button_data(markup)


def test_the_product_screen_shows_the_card_not_a_summary() -> None:
    text, _markup = build_product_view(_product(3))

    assert "💰" in text
    assert "🎯" in text  # threshold
    assert "📌" in text  # initial price and the change since


def test_a_product_with_no_url_offers_no_open_button() -> None:
    _text, markup = build_product_view({**_product(3), "url": ""})

    assert not [b for b in _button_text(markup) if "Open" in b]


# ── Navigating between them ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tapping_a_product_opens_its_screen() -> None:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=_product(3))
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())
    context = MagicMock(user_data={})
    context.bot_data = {"db": db}

    handled = await handle_list_navigation(query, context, db, 7, f"{PRODUCT_PREFIX}3")

    assert handled is True
    assert "Widget 3" in query.edit_message_text.await_args.args[0]
    assert "edit_3" in _button_data(query.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_another_users_product_is_not_opened() -> None:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=None)
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())
    context = MagicMock(user_data={})
    context.bot_data = {"db": db}

    await handle_list_navigation(query, context, db, 7, f"{PRODUCT_PREFIX}3")

    assert "not found" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_paging_edits_in_place_and_rereads_the_products() -> None:
    products = [_product(i) for i in range(1, 25)]
    db = AsyncMock(get_active_products=AsyncMock(return_value=products))
    db.get_all_products = AsyncMock(return_value=products)
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    await handle_list_navigation(query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}1")

    db.get_active_products.assert_awaited_once_with(7)
    query.message.reply_text.assert_not_called()
    assert "#11" in " ".join(
        _button_text(query.edit_message_text.await_args.kwargs["reply_markup"])
    )


@pytest.mark.asyncio
async def test_re_rendering_the_same_page_is_not_an_error() -> None:
    """Tapping the page number redraws what is already there."""
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1)]))
    db.get_all_products = AsyncMock(return_value=[_product(1)])
    query = MagicMock(
        edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
        message=MagicMock(message_id=1),
    )

    assert await handle_list_navigation(
        query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}0"
    )


@pytest.mark.asyncio
async def test_a_tampered_page_falls_back_to_the_first() -> None:
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1), _product(2)]))
    db.get_all_products = AsyncMock(return_value=[])
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    handled = await handle_list_navigation(
        query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}not-a-number"
    )

    assert handled is True
    assert "#1" in " ".join(_button_text(query.edit_message_text.await_args.kwargs["reply_markup"]))


# ── The index the menu opens, and the extras on it ───────────────────────


@pytest.mark.asyncio
async def test_opening_the_index_records_it_as_the_one_a_number_steers() -> None:
    products = [_product(i) for i in range(1, 4)]
    db = AsyncMock(get_active_products=AsyncMock(return_value=products))
    db.get_all_products = AsyncMock(return_value=products)
    query = MagicMock(message=MagicMock(message_id=77), edit_message_text=AsyncMock())
    context = MagicMock(user_data={})

    await handle_list_navigation(query, context, db, 7, f"{LIST_GOTO_PREFIX}0")

    assert context.user_data["list_message_id"] == 77


@pytest.mark.asyncio
async def test_the_index_offers_a_way_to_add_a_product() -> None:
    products = [_product(1)]
    db = AsyncMock(get_active_products=AsyncMock(return_value=products))
    db.get_all_products = AsyncMock(return_value=products)
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())

    await handle_list_navigation(query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}0")

    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "menu_add" in _button_data(markup)
    assert "menu_paused" not in _button_data(markup)


@pytest.mark.asyncio
async def test_paused_products_are_reachable_from_the_index() -> None:
    """The index shows the active ones, so the paused would be invisible."""
    active = [_product(1)]
    query = MagicMock(message=MagicMock(message_id=1), edit_message_text=AsyncMock())
    db = AsyncMock(get_active_products=AsyncMock(return_value=active))
    db.get_all_products = AsyncMock(
        return_value=[*active, {**_product(2), "is_active": 0}, {**_product(3), "is_active": 0}]
    )

    await handle_list_navigation(query, MagicMock(user_data={}), db, 7, f"{LIST_GOTO_PREFIX}0")

    assert "menu_paused" in _button_data(query.edit_message_text.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_cmd_list_sends_exactly_one_message() -> None:
    db = AsyncMock(get_active_products=AsyncMock(return_value=[_product(1), _product(2)]))
    db.is_user_allowed = AsyncMock(return_value=True)
    db.update_user_info = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 7
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock(return_value=MagicMock(message_id=5))
    context = MagicMock(user_data={})
    context.bot_data = {"db": db}

    await cmd_list(update, context)

    update.message.reply_text.assert_awaited_once()


# ── Closing ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_deletes_the_message_and_forgets_the_index() -> None:
    query = MagicMock(message=MagicMock(message_id=555, delete=AsyncMock()))
    context = MagicMock(user_data={"list_message_id": 555})

    handled = await handle_close(query, context, CLOSE_CALLBACK)

    assert handled is True
    query.message.delete.assert_awaited_once()
    assert "list_message_id" not in context.user_data


@pytest.mark.asyncio
async def test_closing_another_panel_keeps_the_index_reference() -> None:
    """The same button closes other panels; forgetting would strand the index."""
    query = MagicMock(message=MagicMock(message_id=999, delete=AsyncMock()))
    context = MagicMock(user_data={"list_message_id": 555})

    await handle_close(query, context, CLOSE_CALLBACK)

    assert context.user_data["list_message_id"] == 555


@pytest.mark.asyncio
async def test_close_collapses_when_the_message_is_too_old_to_delete() -> None:
    """Telegram only lets a bot delete its own messages for 48 hours."""
    query = MagicMock(
        message=MagicMock(delete=AsyncMock(side_effect=BadRequest("message can't be deleted"))),
        edit_message_text=AsyncMock(),
    )

    handled = await handle_close(query, MagicMock(user_data={}), CLOSE_CALLBACK)

    assert handled is True
    assert "closed" in query.edit_message_text.await_args.args[0].lower()


# ── Typing an id ─────────────────────────────────────────────────────────


def _text_update(text: str) -> Any:
    update = MagicMock()
    update.effective_user.id = 7
    update.effective_user.language_code = "en"
    update.effective_chat.id = 1
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _text_context(products: list[dict[str, Any]], **user_data: Any) -> Any:
    db = AsyncMock(get_active_products=AsyncMock(return_value=products))
    db.is_user_allowed = AsyncMock(return_value=True)
    db.update_user_info = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=products[0] if products else None)
    context = MagicMock()
    context.user_data = dict(user_data)
    context.bot_data = {"db": db, "config": MagicMock(check_interval_minutes=360)}
    context.bot.edit_message_text = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_typing_an_id_opens_that_product() -> None:
    """The buttons say `#3`, so `3` is what the screen invites you to type."""
    from price_tracker.bot.handlers.text_input import handle_text_input

    context = _text_context([_product(3)], list_message_id=555)

    await handle_text_input(_text_update("3"), context)

    kwargs = context.bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 555
    assert "<b>#3</b>" in kwargs["text"]


@pytest.mark.asyncio
async def test_typing_an_id_you_do_not_have_says_so() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input

    update = _text_update("99")
    context = _text_context([], list_message_id=555)

    await handle_text_input(update, context)

    context.bot.edit_message_text.assert_not_awaited()
    assert "No product #99" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_a_number_answering_a_prompt_still_goes_to_the_prompt() -> None:
    """A pending picker wins: typing 30 for /refresh must not open a product."""
    context = _text_context([_product(1)], list_message_id=555)
    context.user_data["pending_action"] = PendingInput("refresh", 1)
    context.bot_data["db"].set_product_interval = AsyncMock()
    from price_tracker.bot.handlers.text_input import handle_text_input

    await handle_text_input(_text_update("30"), context)

    context.bot.edit_message_text.assert_not_awaited()
    context.bot_data["db"].set_product_interval.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_number_without_an_open_index_is_ignored() -> None:
    from price_tracker.bot.handlers.text_input import handle_text_input

    update = _text_update("3")
    context = _text_context([_product(3)])

    await handle_text_input(update, context)

    context.bot.edit_message_text.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_index_reference_is_dropped() -> None:
    """If the index was deleted, forget it instead of swallowing later numbers."""
    from price_tracker.bot.handlers.text_input import handle_text_input

    context = _text_context([_product(3)], list_message_id=555)
    context.bot.edit_message_text = AsyncMock(side_effect=BadRequest("message to edit not found"))

    await handle_text_input(_text_update("3"), context)

    assert "list_message_id" not in context.user_data
