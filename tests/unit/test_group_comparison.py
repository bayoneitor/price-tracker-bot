"""Comparing a group: the table, and who has been cheapest over time.

Tracking three monitors told you each one's price and nothing about how they
stood against each other. The "who was cheapest" answer is derived from the
price history already on disk rather than recorded as the comparison runs, so a
group created today covers the whole period its members have been tracked.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from price_tracker.bot.handlers.groups_view import (
    build_comparison_table,
    build_leader_timeline,
    render_leader_timeline,
)

START = datetime(2026, 6, 1, 12, 0)


def _reading(day: int, price: str) -> dict[str, Any]:
    return {"checked_at": (START + timedelta(days=day)).isoformat(), "price": price}


def _product(pid: int, name: str, current: str | None, **extra: Any) -> dict[str, Any]:
    return {"id": pid, "name": name, "current_price": current, **extra}


# ── The table ────────────────────────────────────────────────────────────


def test_the_table_is_ordered_cheapest_first_and_states_the_spread() -> None:
    text = build_comparison_table(
        [
            _product(1, "Dear", "300"),
            _product(2, "Cheap", "200"),
            _product(3, "Middle", "250"),
        ]
    )

    assert text.index("Cheap") < text.index("Middle") < text.index("Dear")
    assert "Spread: €100.00" in text
    assert "Cheapest now" in text


def test_the_table_shows_the_change_since_tracking_began() -> None:
    text = build_comparison_table(
        [
            _product(1, "Fell", "80", initial_price="100"),
            _product(2, "Rose", "120", initial_price="100"),
        ]
    )

    assert "-20.0%" in text
    assert "+20.0%" in text


def test_a_product_with_no_price_is_listed_rather_than_dropped() -> None:
    text = build_comparison_table([_product(1, "Known", "50"), _product(2, "Unread", None)])

    assert "Unread" in text
    assert "no price yet" in text


def test_one_product_is_not_a_comparison() -> None:
    assert "at least two" in build_comparison_table([_product(1, "Alone", "50")])


# ── Who has been cheapest ────────────────────────────────────────────────


def test_the_leader_is_carried_forward_between_readings() -> None:
    """Members are not checked in lockstep: comparing only same-instant readings
    would compare almost nothing, since at any moment most have no reading."""
    spans = build_leader_timeline(
        {
            1: [_reading(0, "100"), _reading(4, "95")],
            2: [_reading(2, "80")],
        }
    )

    # #1 leads from its first reading, then #2 undercuts it and keeps the lead.
    assert [(s.product_id, str(s.price)) for s in spans] == [(1, "100"), (2, "80")]


def test_a_price_move_that_does_not_change_the_leader_is_not_an_event() -> None:
    spans = build_leader_timeline({1: [_reading(0, "100"), _reading(1, "99"), _reading(2, "98")]})

    assert [str(s.price) for s in spans] == ["100", "99", "98"]
    assert {s.product_id for s in spans} == {1}


def test_the_lead_changing_back_is_recorded() -> None:
    spans = build_leader_timeline(
        {
            1: [_reading(0, "100"), _reading(3, "70")],
            2: [_reading(1, "80")],
        }
    )

    assert [s.product_id for s in spans] == [1, 2, 1]


def test_a_tie_names_one_leader_and_says_it_was_shared() -> None:
    """Otherwise equal prices would read as the lead flapping back and forth."""
    spans = build_leader_timeline({1: [_reading(0, "100")], 2: [_reading(1, "100")]})

    assert spans[-1].product_id == 1
    assert spans[-1].tied_with == (2,)


def test_series_of_different_lengths_are_handled() -> None:
    spans = build_leader_timeline(
        {
            1: [_reading(day, str(100 - day)) for day in range(10)],
            2: [_reading(0, "200")],
            3: [],
        }
    )

    assert all(s.product_id == 1 for s in spans)


def test_unreadable_rows_are_skipped_not_fatal() -> None:
    spans = build_leader_timeline(
        {1: [{"checked_at": "not a date", "price": "10"}, _reading(0, "50")]}
    )

    assert [str(s.price) for s in spans] == ["50"]


def test_no_history_says_so_instead_of_rendering_an_empty_list() -> None:
    assert "Not enough history" in render_leader_timeline([], {})


def test_the_timeline_is_rendered_with_names_and_dates() -> None:
    spans = build_leader_timeline({1: [_reading(0, "100")], 2: [_reading(1, "80")]})

    rendered = render_leader_timeline(spans, {1: "Alpha", 2: "Beta"})

    assert "Alpha" in rendered
    assert "Beta" in rendered
    assert START.strftime("%d/%m") in rendered


def test_a_long_history_is_trimmed_to_the_recent_changes() -> None:
    histories: dict[int, list[dict[str, Any]]] = {1: [], 2: []}
    for day in range(40):
        # The two keep undercutting each other, one change per reading.
        histories[1 + day % 2].append(_reading(day, str(Decimal(100 - day))))

    rendered = render_leader_timeline(
        build_leader_timeline(histories), {1: "Alpha", 2: "Beta"}, limit=5
    )

    assert "earlier changes not shown" in rendered
