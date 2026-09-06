"""Chart rendering must run off the event loop (#13).

The renderer did all the matplotlib work synchronously in the async handler,
blocking the whole bot for the render duration. The heavy work now runs in a
worker thread via asyncio.to_thread — for the comparison chart too, which draws
several series and takes correspondingly longer.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from price_tracker.bot import charts

if TYPE_CHECKING:
    import io
    from datetime import datetime


async def test_chart_renders_off_the_event_loop(monkeypatch) -> None:
    main_thread = threading.get_ident()
    captured: dict[str, int] = {}
    real_render = charts._render_chart

    def spy(dates: list[datetime], prices: list[float], target: object, name: str) -> io.BytesIO:
        captured["thread"] = threading.get_ident()
        return real_render(dates, prices, target, name)

    monkeypatch.setattr(charts, "_render_chart", spy)

    db = AsyncMock()
    db.get_price_change_points = AsyncMock(
        return_value={
            1: [
                {"checked_at": "2026-06-01T10:00:00", "price": "100"},
                {"checked_at": "2026-06-02T10:00:00", "price": "90"},
            ]
        }
    )

    buf = await charts.generate_chart(db, 1, {"name": "Widget"})

    assert buf is not None
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG
    assert captured["thread"] != main_thread  # rendered in a worker thread


async def test_comparison_chart_also_renders_off_the_event_loop(monkeypatch) -> None:
    main_thread = threading.get_ident()
    captured: dict[str, Any] = {}
    real_render = charts._render_comparison

    def spy(series: list[tuple[str, list[datetime], list[float]]], title: str) -> io.BytesIO:
        captured["thread"] = threading.get_ident()
        captured["aliases"] = [alias for alias, _dates, _prices in series]
        return real_render(series, title)

    monkeypatch.setattr(charts, "_render_comparison", spy)

    db = AsyncMock()
    db.get_price_change_points = AsyncMock(
        return_value={
            1: [
                {"checked_at": "2026-06-01T10:00:00", "price": "100"},
                {"checked_at": "2026-06-02T10:00:00", "price": "90"},
            ],
            2: [
                {"checked_at": "2026-06-01T10:00:00", "price": "120"},
                {"checked_at": "2026-06-02T10:00:00", "price": "119"},
            ],
        }
    )

    rendered = await charts.generate_comparison_chart(
        db, [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}], "Monitors"
    )

    assert rendered is not None
    buf, drawn = rendered
    assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    assert captured["thread"] != main_thread
    # The chart is drawn under aliases; the caller spells them out beneath it.
    assert captured["aliases"] == ["A", "B"]
    assert [alias for alias, _product in drawn] == ["A", "B"]
    assert [p["name"] for _alias, p in drawn] == ["One", "Two"]
