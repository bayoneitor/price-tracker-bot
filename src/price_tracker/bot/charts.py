"""Price charts: one product over time, or a group compared side by side.

`history.py` carried the renderer with a note that it belonged in its own module
once there was more than one kind of chart. There is now: the group comparison
draws a line per member on shared axes.

Both renderers use the matplotlib OO API (Figure, not pyplot) so concurrent
renders in the threadpool cannot race on pyplot's global figure registry, and both
run through `asyncio.to_thread` so drawing never blocks the event loop — and with
it every other user — while it happens. matplotlib itself is imported lazily to
keep startup fast.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

SURFACE = "#000000"
INK = "#ffffff"
INK_MUTED = "#999999"
GRID = "#555555"
AXIS = "#333333"

# One product's own history: a single series needs no legend, so it keeps the
# accent colour it has always had.
SINGLE_SERIES = "#ff9f1c"
TARGET_LINE = "#ff6b6b"

# Categorical slots for the comparison, in fixed order and never cycled: colour
# follows the product, so filtering the group does not repaint the survivors.
# Validated as a set against this surface — every adjacent pair clears the
# colour-vision-deficiency and normal-vision separation floors and 3:1 contrast.
SERIES_COLOURS = (
    "#3987e5",  # blue
    "#d95926",  # orange
    "#199e70",  # aqua
    "#c98500",  # yellow
    "#d55181",  # magenta
    "#008300",  # green
    "#9085e9",  # violet
    "#e66767",  # red
)
# Past eight the palette would have to cycle, and two products sharing a colour
# is worse than not drawing one of them.
MAX_SERIES = len(SERIES_COLOURS)

# Below this, naming each line beside it beats making the reader match a legend.
DIRECT_LABEL_LIMIT = 4


def _style_axes(fig: Any, ax: Any, title: str) -> None:
    """The shared chart furniture: dark surface, recessive axes, muted grid."""
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_ylabel("€", color=INK, fontsize=10)
    ax.tick_params(colors=INK_MUTED, labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(AXIS)
    ax.spines["bottom"].set_color(AXIS)
    ax.grid(axis="y", alpha=0.15, color=GRID)
    ax.set_title(title, color=INK, fontsize=10, pad=10)


def _to_png(fig: Any) -> io.BytesIO:
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf


def _render_chart(
    dates: list[datetime], prices: list[float], target: object, name: str
) -> io.BytesIO:
    """Render one product's price history to a PNG buffer (pure CPU — via to_thread)."""
    import matplotlib  # noqa: PLC0415 — heavy import deferred

    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    from matplotlib.figure import Figure  # noqa: PLC0415

    fig = Figure(figsize=(8, 3.5), dpi=100)
    ax = fig.subplots()
    _style_axes(fig, ax, name)

    ax.plot(dates, prices, color=SINGLE_SERIES, linewidth=2.2, antialiased=True)

    if target:
        try:
            target_f = float(target)
            ax.axhline(
                y=target_f,
                color=TARGET_LINE,
                linestyle="--",
                linewidth=1,
                alpha=0.8,
                label=f"Target €{target_f:.2f}",
            )
            ax.legend(facecolor=SURFACE, edgecolor=AXIS, labelcolor=INK, fontsize=8)
        except (ValueError, TypeError):
            pass

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate(rotation=30)

    min_p, max_p = min(prices), max(prices)
    margin = (max_p - min_p) * 0.15 if max_p != min_p else max_p * 0.05
    ax.set_ylim(min_p - margin, max_p + margin)
    return _to_png(fig)


def _render_comparison(
    series: list[tuple[str, list[datetime], list[float]]], title: str
) -> io.BytesIO:
    """Render several products on shared axes (pure CPU — via to_thread).

    One y-axis for every series: they are all prices in euro, so a second scale
    would only make two products look closer or further apart than they are.
    """
    import matplotlib  # noqa: PLC0415 — heavy import deferred

    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    from matplotlib.figure import Figure  # noqa: PLC0415

    fig = Figure(figsize=(9, 4.2), dpi=100)
    ax = fig.subplots()
    _style_axes(fig, ax, title)

    for index, (name, dates, prices) in enumerate(series):
        colour = SERIES_COLOURS[index]
        ax.plot(dates, prices, color=colour, linewidth=2, antialiased=True, label=name)
        if len(series) <= DIRECT_LABEL_LIMIT:
            # A coloured dot carries the identity; the name itself stays in the
            # text colour so it reads as a label and not as another mark.
            ax.plot(dates[-1], prices[-1], marker="o", markersize=6, color=colour)
            ax.annotate(
                name,
                xy=(dates[-1], prices[-1]),
                xytext=(6, 0),
                textcoords="offset points",
                color=INK,
                fontsize=8,
                va="center",
            )

    if len(series) <= DIRECT_LABEL_LIMIT:
        # Room on the right for the direct labels, which would otherwise run off
        # the edge of the plot.
        left, right = ax.get_xlim()
        ax.set_xlim(left, right + (right - left) * 0.22)

    # Always present with two or more series, so identity is never colour alone.
    # Below the plot rather than inside it: at eight series a boxed legend covers
    # the very lines it is naming.
    ax.legend(
        labelcolor=INK,
        fontsize=8,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=min(4, len(series)),
        frameon=False,
    )
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate(rotation=30)
    return _to_png(fig)


async def generate_chart(db: Any, product_id: int, product: dict[str, Any]) -> io.BytesIO | None:
    """One product's price history as a PNG. None when the data is too sparse."""
    history = await db.get_price_history(product_id, limit=100)
    dates, prices = _points(history)
    if len(dates) < 2:
        return None

    name = (product.get("name") or _("Product"))[:50]
    return await asyncio.to_thread(_render_chart, dates, prices, product.get("target_price"), name)


async def generate_comparison_chart(
    db: Any, products: Sequence[dict[str, Any]], title: str
) -> io.BytesIO | None:
    """A group's members on one chart. None when fewer than two have a history.

    Prices are converted to euro first: the members can be tracked on stores in
    different currencies, and drawing those raw on one axis would compare numbers
    that are not comparable.
    """
    ids = [int(p["id"]) for p in products][:MAX_SERIES]
    if len(ids) < 2:
        return None
    histories: Mapping[int, Sequence[Any]] = await db.get_price_history_for_products(ids)
    by_id = {int(p["id"]): p for p in products}

    series: list[tuple[str, list[datetime], list[float]]] = []
    for product_id in ids:
        product = by_id[product_id]
        dates, prices = _points(histories.get(product_id, ()), product.get("currency", "EUR"))
        if len(dates) < 2:
            continue
        series.append(((product.get("name") or _("Product"))[:28], dates, prices))

    if len(series) < 2:
        return None
    return await asyncio.to_thread(_render_comparison, series, title)


def _points(history: Sequence[Any], currency: str = "EUR") -> tuple[list[datetime], list[float]]:
    """Readings as parallel (date, euro price) lists, oldest first, bad rows dropped."""
    from price_tracker.bot.decorators import _get_conversion_rate  # noqa: PLC0415

    rate = Decimal(1) if currency in ("", "EUR") else (_get_conversion_rate(currency) or Decimal(1))
    dates: list[datetime] = []
    prices: list[float] = []
    for record in history:
        try:
            when = datetime.fromisoformat(str(record["checked_at"]).replace("Z", "+00:00"))
            price = float(Decimal(str(record["price"])) * rate)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
        dates.append(when)
        prices.append(price)

    # The single-product query returns newest first; the multi-product one oldest
    # first. Sorting here means neither caller has to care.
    ordered = sorted(zip(dates, prices, strict=True), key=lambda point: point[0])
    return [d for d, _p in ordered], [p for _d, p in ordered]
