"""How a product is named on screen.

The name alone stopped being enough once the same product could be tracked at
several shops: three rows reading "LG 27GP850-B" tell you nothing about which is
which. Every place that names a product uses `product_label`, so the shop is
always there and always in the same position.

Nothing is cut by default. A budget is passed only where the width is genuinely
fixed — inside a chart, where a plot is centimetres wide — and there the label is
not used at all: series get an alias and the names go in the caption. Everywhere
else the reader gets the whole name, and where a budget *is* given the shop is
what survives it, being the part that distinguishes otherwise identical rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import _
from price_tracker.core.textlimits import truncate_visible
from price_tracker.core.url_utils import store_label

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Between the product and its shop. A middot rather than a dash: product names
#: are full of dashes already, and it has to read as a boundary.
SEPARATOR = " · "


def product_label(product: Mapping[str, Any] | Any, budget: int | None = None) -> str:
    """``Product name · shop.com``, whole unless a `budget` is given.

    Falls back to the bare name when the shop is unknown — a row written before
    the domain column existed, or a URL nothing could be derived from.
    """
    name = str(product.get("name") or _("Unknown")).strip()
    shop = store_label(url=product.get("url") or "", domain=product.get("domain") or "")
    label = f"{name}{SEPARATOR}{shop}" if shop else name
    if budget is None or len(label) <= budget:
        return label

    if not shop:
        return truncate_visible(name, budget)
    tail = f"{SEPARATOR}{shop}"
    if len(tail) >= budget:
        # No room for both: the shop is the more useful half of the pair here,
        # since it is what a duplicate row differs by.
        return truncate_visible(shop, budget)
    return f"{truncate_visible(name, budget - len(tail))}{tail}"


def chart_title(product: Mapping[str, Any] | Any) -> str:
    """What goes *on* a single-product chart: ``#3 · shop.com``.

    Deliberately not the product name. A name long enough to be interesting is
    long enough to be truncated at this width, and the caption underneath can
    carry it whole — so the image gets the two parts that are always short, and
    nothing on screen is cut.
    """
    pid = product.get("id")
    shop = store_label(url=product.get("url") or "", domain=product.get("domain") or "")
    if not shop:
        return f"#{pid}"
    return f"#{pid}{SEPARATOR}{shop}"
