"""A trail must not outlive what it points at.

"Back" re-dispatches a stored callback token, and the dispatcher pushes every
token onto the trail — so deleting a product left `prod_5`, `remove_5` and
`confirm_delete_5` sitting in the trail of the very message that had just
confirmed the deletion. The next ◀️ Back re-rendered the delete confirmation for
a product that was gone, and the bot answered "❌ Product not found." one tap
after telling the reader it had deleted it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers._helpers import resolve_owned_product
from price_tracker.bot.handlers.callbacks._product import handle_delete_flow
from price_tracker.bot.navigation import NAV_KEY, forget_product


def _ctx(trails: dict[int, list[str]]) -> Any:
    return SimpleNamespace(user_data={NAV_KEY: {k: list(v) for k, v in trails.items()}})


def _trail(ctx: Any, message_id: int) -> Any:
    return ctx.user_data[NAV_KEY][message_id]


# ── The sweep itself ─────────────────────────────────────────────────────


def test_every_screen_of_the_product_comes_out() -> None:
    ctx = _ctx({10: ["list_go_0", "prod_5", "remove_5", "confirm_delete_5"]})

    forget_product(ctx, 5)

    assert _trail(ctx, 10) == ["list_go_0"]


def test_the_screens_of_other_products_stay() -> None:
    ctx = _ctx({10: ["list_go_0", "prod_4", "prod_5", "edit_4"]})

    forget_product(ctx, 5)

    assert _trail(ctx, 10) == ["list_go_0", "prod_4", "edit_4"]


def test_a_group_sharing_the_number_is_not_a_product() -> None:
    """`grp_open_5` is group 5. Only product screens carry a product id."""
    ctx = _ctx({10: ["menu_main", "grp_open_5", "prod_5"]})

    forget_product(ctx, 5)

    assert _trail(ctx, 10) == ["menu_main", "grp_open_5"]


def test_a_page_number_is_not_a_product_id() -> None:
    """`list_go_5` is the sixth page of the listing, not a screen for product 5."""
    ctx = _ctx({10: ["list_go_5", "prod_5"]})

    forget_product(ctx, 5)

    assert _trail(ctx, 10) == ["list_go_5"]


def test_every_panel_is_swept_not_only_the_one_in_front_of_the_reader() -> None:
    """The same product may be open in another panel further up the chat."""
    ctx = _ctx({10: ["list_go_0", "prod_5"], 11: ["menu_main", "chart_5"]})

    forget_product(ctx, 5)

    assert _trail(ctx, 10) == ["list_go_0"]
    assert _trail(ctx, 11) == ["menu_main"]


def test_a_context_with_no_trail_is_not_an_error() -> None:
    empty: Any = SimpleNamespace(user_data=None)
    forget_product(empty, 5)  # must not raise


# ── Deleting through the confirmation ────────────────────────────────────


def _delete_context(trail: list[str]) -> tuple[Any, Any, Any]:
    db = AsyncMock()
    db.delete_product = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value={"id": 5, "name": "Widget"})

    context = MagicMock()
    context.user_data = {NAV_KEY: {10: list(trail)}}
    context.bot_data = {"db": db}

    query = MagicMock(message=MagicMock(message_id=10), edit_message_text=AsyncMock())
    return query, context, db


@pytest.mark.asyncio
async def test_confirming_a_deletion_clears_the_products_trail() -> None:
    query, context, db = _delete_context(["list_go_0", "prod_5", "remove_5"])

    handled = await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    assert handled is True
    db.delete_product.assert_awaited_once()
    assert context.user_data[NAV_KEY][10] == ["list_go_0"]


@pytest.mark.asyncio
async def test_back_after_a_deletion_leads_to_the_listing() -> None:
    """What the reader actually presses: the trail must offer a live screen."""
    from price_tracker.bot.navigation import previous_nav

    query, context, db = _delete_context(["list_go_0", "prod_5", "remove_5"])
    # The dispatcher pushes the token it is about to run.
    context.user_data[NAV_KEY][10].append("confirm_delete_5")

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    assert previous_nav(context, 10) is None
    assert context.user_data[NAV_KEY][10] == ["list_go_0"]


@pytest.mark.asyncio
async def test_deleting_everything_clears_every_products_trail() -> None:
    db = AsyncMock()
    db.get_active_products = AsyncMock(side_effect=[[{"id": 4}, {"id": 5}], []])
    db.get_all_products = AsyncMock(return_value=[])
    db.delete_product = AsyncMock(return_value=True)
    context = MagicMock()
    context.user_data = {NAV_KEY: {10: ["menu_main", "prod_4", "prod_5"]}}
    query = MagicMock(message=MagicMock(message_id=10), edit_message_text=AsyncMock())

    await handle_delete_flow(query, context, db, 1, "confirmdeleteall")

    # The product screens are gone and the listing this message now shows is on
    # top, so ◀️ Back leads to the menu rather than to a deleted product.
    assert context.user_data[NAV_KEY][10] == ["menu_main", "list_go_0"]


# ── Where a deletion leaves the reader ───────────────────────────────────


def _index_context(remaining: list[dict[str, Any]]) -> tuple[Any, Any, Any]:
    db = AsyncMock()
    db.delete_product = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value={"id": 5, "name": "Widget"})
    db.get_active_products = AsyncMock(return_value=remaining)
    db.get_all_products = AsyncMock(return_value=remaining)

    context = MagicMock()
    context.user_data = {NAV_KEY: {10: ["menu_main", "list_go_0", "prod_5", "remove_5"]}}
    context.bot_data = {"db": db}
    query = MagicMock(message=MagicMock(message_id=10), edit_message_text=AsyncMock())
    return query, context, db


def _listed(item: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "id": 4,
            "name": "Still here",
            "domain": "amazon.es",
            "url": "https://amazon.es/p/4",
            "current_price": "10.00",
            "currency": "EUR",
        }
    ] + ([item] if item else [])


@pytest.mark.asyncio
async def test_deleting_lands_back_on_the_listing() -> None:
    """Deleting is tidying a list, so the list is where the reader wants to be."""
    query, context, db = _index_context(_listed())

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    text = query.edit_message_text.await_args.args[0]
    assert "Your products" in text
    assert "#4" in text


@pytest.mark.asyncio
async def test_the_listing_still_says_what_was_deleted() -> None:
    query, context, db = _index_context(_listed())

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    assert "Widget" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_the_deleted_product_is_not_in_the_listing_it_lands_on() -> None:
    """The listing is the proof, not just the destination."""
    query, context, db = _index_context(_listed())

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "prod_5" not in [
        b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data
    ]


@pytest.mark.asyncio
async def test_back_from_there_leaves_the_listing_not_the_product() -> None:
    query, context, db = _index_context(_listed())

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    assert context.user_data[NAV_KEY][10] == ["menu_main", "list_go_0"]


@pytest.mark.asyncio
async def test_a_typed_number_steers_the_listing_it_lands_on() -> None:
    from price_tracker.bot.handlers.product_list import LIST_MESSAGE_KEY

    query, context, db = _index_context(_listed())

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    assert context.user_data[LIST_MESSAGE_KEY] == 10


@pytest.mark.asyncio
async def test_deleting_the_last_product_lands_on_the_empty_listing() -> None:
    query, context, db = _index_context([])

    await handle_delete_flow(query, context, db, 1, "confirm_delete_5")

    assert "no tracked products" in query.edit_message_text.await_args.args[0]


# ── The self-healing path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_product_found_missing_is_swept_out_of_the_trail() -> None:
    """Deleted from another panel, or by a prefix the sweep does not know."""
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=None)
    context = MagicMock()
    context.user_data = {NAV_KEY: {10: ["list_go_0", "prod_5", "edit_5"]}}
    context.bot_data = {"db": db}
    query = MagicMock(message=MagicMock(message_id=10), edit_message_text=AsyncMock())

    assert await resolve_owned_product(query, context, "edit_5", "edit_", 1) is None
    assert context.user_data[NAV_KEY][10] == ["list_go_0"]
