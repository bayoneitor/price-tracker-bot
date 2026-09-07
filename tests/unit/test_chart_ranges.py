"""The row of ranges under a chart, and the picture that changes in place.

A chart opens on everything the product remembers — the only window nobody has
to guess at — and the buttons narrow it without leaving the message. Swapping
the media rather than sending a second photo is what keeps the message id, and
therefore the navigation trail, pointing at the panel the chart came from.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.handlers.callbacks import _chart
from price_tracker.bot.navigation import NAV_KEY, previous_nav, push_nav

AMAZON = "https://www.amazon.es/dp/B0CX23V2ZK"


def _png() -> io.BytesIO:
    return io.BytesIO(b"\x89PNG\r\n\x1a\n fake")


def _query(*, photo: bool = False, markup: Any = None) -> MagicMock:
    message = MagicMock(message_id=10)
    message.delete = AsyncMock()
    message.reply_photo = AsyncMock(return_value=MagicMock(message_id=20))
    message.photo = (MagicMock(),) if photo else ()
    message.reply_markup = markup
    return MagicMock(
        message=message,
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_media=AsyncMock(),
    )


def _product(**extra: Any) -> dict[str, Any]:
    return {
        "id": 5,
        "name": "Monitor",
        "url": "https://www.pccomponentes.com/monitor",
        "domain": "pccomponentes.com",
        "currency": "EUR",
        **extra,
    }


def _db(product: dict[str, Any] | None = None) -> AsyncMock:
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value=product if product is not None else _product())
    db.get_price_change_points = AsyncMock(return_value={})
    db.lowest_price_point = AsyncMock(return_value=None)
    return db


def _context(db: AsyncMock) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db}
    context.user_data = {NAV_KEY: {10: ["prod_5"]}}
    return context


def _sent_markup(query: MagicMock) -> Any:
    return _edited_markup(query.message.reply_photo)


def _edited_markup(call: Any) -> Any:
    return _kwargs(call)["reply_markup"]


def _kwargs(call: Any) -> Any:
    """The keyword arguments of a mock's one awaited call."""
    assert call.await_args is not None, "expected this to have been awaited"
    return call.await_args.kwargs


def _texts(markup: Any) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _tokens(markup: Any) -> list[str]:
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


# ── Opening the chart ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_chart_opens_on_everything_the_product_remembers() -> None:
    db = _db()
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())) as chart:
        await _chart.handle_chart_button(_query(), _context(db), db, 1, "chart_5")

    assert _kwargs(chart)["days"] is None


@pytest.mark.asyncio
async def test_the_four_ranges_sit_under_the_chart_with_all_marked() -> None:
    db = _db()
    query = _query()
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())):
        await _chart.handle_chart_button(query, _context(db), db, 1, "chart_5")

    markup = _sent_markup(query)
    assert [t for t in _tokens(markup) if t.startswith("chartr|")] == [
        "chartr|5|30d",
        "chartr|5|6m",
        "chartr|5|1y",
        "chartr|5|all",
    ]
    marked = [text for text in _texts(markup) if text.startswith(_chart.ACTIVE_MARK)]
    assert len(marked) == 1
    assert marked[0].endswith("All")


@pytest.mark.asyncio
async def test_keepa_is_offered_on_the_chart_of_an_amazon_product() -> None:
    """The graph Keepa draws itself, beside the one this bot draws."""
    db = _db(_product(url=AMAZON))
    query = _query()
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())):
        await _chart.handle_chart_button(query, _context(db), db, 1, "chart_5")

    assert "keepa_5" in _tokens(_sent_markup(query))


@pytest.mark.asyncio
async def test_no_keepa_button_without_an_asin() -> None:
    db = _db()
    query = _query()
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())):
        await _chart.handle_chart_button(query, _context(db), db, 1, "chart_5")

    assert not any(token.startswith("keepa_") for token in _tokens(_sent_markup(query)))


# ── Switching range ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("token", "days"), [("30d", 30), ("6m", 182), ("1y", 365), ("all", None)])
async def test_each_button_asks_for_its_own_window(token: str, days: int | None) -> None:
    db = _db()
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())) as chart:
        handled = await _chart.handle_chart_range(
            _query(photo=True), _context(db), db, 1, f"chartr|5|{token}"
        )

    assert handled is True
    assert _kwargs(chart)["days"] == days


@pytest.mark.asyncio
async def test_the_picture_is_swapped_in_place_not_sent_again() -> None:
    """A second photo per tap would bury the chat under one product at four zooms."""
    db = _db()
    query = _query(photo=True)
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())):
        await _chart.handle_chart_range(query, _context(db), db, 1, "chartr|5|30d")

    query.edit_message_media.assert_awaited_once()
    query.message.reply_photo.assert_not_awaited()
    query.message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_range_you_tapped_is_the_one_marked_afterwards() -> None:
    db = _db()
    query = _query(photo=True)
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=_png())):
        await _chart.handle_chart_range(query, _context(db), db, 1, "chartr|5|6m")

    markup = _edited_markup(query.edit_message_media)
    marked = [text for text in _texts(markup) if text.startswith(_chart.ACTIVE_MARK)]
    assert len(marked) == 1
    assert marked[0].endswith("6 months")


@pytest.mark.asyncio
async def test_an_unknown_range_is_not_claimed() -> None:
    """Tampered callback data falls through to the dispatcher's own logging
    rather than being answered with a chart of some arbitrary window."""
    db = _db()
    handled = await _chart.handle_chart_range(
        _query(photo=True), _context(db), db, 1, "chartr|5|ever"
    )

    assert handled is False


@pytest.mark.asyncio
async def test_a_tampered_id_says_so_in_the_caption() -> None:
    """`resolve_owned_product` would edit *text*, which raises on a photo."""
    db = _db()
    query = _query(photo=True)

    handled = await _chart.handle_chart_range(query, _context(db), db, 1, "chartr|abc|30d")

    assert handled is True
    query.edit_message_text.assert_not_awaited()
    query.edit_message_caption.assert_awaited_once()


@pytest.mark.asyncio
async def test_someone_elses_product_is_refused() -> None:
    db = _db()
    db.get_product_for_user = AsyncMock(return_value=None)
    query = _query(photo=True)

    handled = await _chart.handle_chart_range(query, _context(db), db, 1, "chartr|5|30d")

    assert handled is True
    query.edit_message_media.assert_not_awaited()


# ── An empty range ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_range_keeps_the_picture_and_explains_itself() -> None:
    """The chart on screen is still an honest drawing of the range it was drawn
    for. Replacing it with an apology would throw away what the reader had."""
    db = _db()
    query = _query(photo=True)
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=None)):
        handled = await _chart.handle_chart_range(query, _context(db), db, 1, "chartr|5|30d")

    assert handled is True
    query.edit_message_media.assert_not_awaited()
    caption = _kwargs(query.edit_message_caption)["caption"]
    assert "range" in caption.lower()


@pytest.mark.asyncio
async def test_an_empty_range_leaves_the_mark_on_the_range_actually_drawn() -> None:
    """Nothing was redrawn, so nothing should claim to be what you are seeing."""
    db = _db()
    drawn = _chart.chart_keyboard(_context(db), 10, 5, "", "1y")
    query = _query(photo=True, markup=drawn)
    with patch.object(_chart, "generate_chart", AsyncMock(return_value=None)):
        await _chart.handle_chart_range(query, _context(db), db, 1, "chartr|5|30d")

    markup = _edited_markup(query.edit_message_caption)
    marked = [text for text in _texts(markup) if text.startswith(_chart.ACTIVE_MARK)]
    assert marked == [_chart.ACTIVE_MARK + "1 year"]


# ── The navigation trail ─────────────────────────────────────────────────


def test_changing_range_replaces_the_chart_on_the_trail_rather_than_stacking() -> None:
    """Otherwise ◀️ Back would walk the reader through every zoom they tried
    before letting them out of the chart."""
    context = MagicMock()
    context.user_data = {}
    push_nav(context, 20, "prod_5")
    push_nav(context, 20, "chart_5")
    push_nav(context, 20, "chartr|5|30d")
    push_nav(context, 20, "chartr|5|1y")

    assert previous_nav(context, 20) == "prod_5"
