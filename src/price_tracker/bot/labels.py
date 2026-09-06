"""How a product is named on screen.

The name alone stopped being enough once the same product could be tracked at
several shops: three rows reading "LG 27GP850-B" tell you nothing about which is
which. Every place that names a product uses `product_label`, so the shop is
always there and always in the same position.

The shop is what survives a tight budget. It is short and it is the part that
distinguishes otherwise identical rows, so the product name is what gets cut.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import _
from price_tracker.core.textlimits import NAME_BUDGET, truncate_visible
from price_tracker.core.url_utils import store_label

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Between the product and its shop. A middot rather than a dash: product names
#: are full of dashes already, and it has to read as a boundary.
SEPARATOR = " · "


def product_label(product: Mapping[str, Any] | Any, budget: int = NAME_BUDGET) -> str:
    """``Product name… · shop.com``, cut to `budget` characters.

    Falls back to the bare name when the shop is unknown — a row written before
    the domain column existed, or a URL nothing could be derived from.
    """
    name = str(product.get("name") or _("Unknown")).strip()
    shop = store_label(url=product.get("url") or "", domain=product.get("domain") or "")
    if not shop:
        return truncate_visible(name, budget)

    tail = f"{SEPARATOR}{shop}"
    if len(tail) >= budget:
        # No room for both: the shop is the more useful half of the pair here,
        # since it is what a duplicate row differs by.
        return truncate_visible(shop, budget)
    return f"{truncate_visible(name, budget - len(tail))}{tail}"
