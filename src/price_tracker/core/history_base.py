"""The seam a history provider plugs into.

Adding a product starts the chart empty and every alert trigger measuring
against a single number: the price it had when the link was pasted. A
provider answers a narrower question than a scraper does — not "what is the
price now" but "what was it before I started watching" — and does it once, at
add time, rather than on every check.

The shape mirrors `AbstractScraper` on purpose: `can_handle` + one fetch
method, registered by priority, first match wins. Nothing here fetches
anything itself. The two providers that do (Keepa, a Spanish price-history
service) are operator-installed plugins, not part of this repository — see
`docs/history-providers.md`. Without any installed, a product is added exactly
as it is today.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    import httpx


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One remembered price, from before the bot itself was watching."""

    observed_at: datetime  # timezone-aware, UTC
    price: Decimal


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """What a provider found, or why it found nothing.

    Empty and successful are not the same outcome — a provider that genuinely
    has no data for a product returns `points=()`; one that failed to look
    sets `error` instead, so a caller logging misses can tell a shrug from a
    fault. Neither one raises: see `AbstractHistoryProvider.fetch`.
    """

    points: tuple[HistoryPoint, ...] = ()
    currency: str | None = None
    error: str | None = None


class AbstractHistoryProvider(ABC):
    """Base class for a price-history backfill source.

    `fetch` must not raise. A provider that cannot answer — no data, a site
    that changed shape, a timeout, no credentials configured — returns a
    `HistoryResult(error=...)` instead. The caller (`_add_product`) walks every
    registered provider in priority order and a raised exception there would
    abort adding the product over a feature that only ever enriches it.
    """

    name: ClassVar[str] = ""
    priority: ClassVar[int] = 0

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return True if this provider might have history for the URL."""

    @abstractmethod
    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult:
        """Look up whatever history exists for `url`. Never raises.

        `client` is the shared httpx client, offered for parity with
        `AbstractScraper.scrape` and for a provider that only needs plain
        HTTP. A provider that drives a browser (both shipped providers do)
        ignores it and launches its own, the way
        `scrapers/playwright_fallback.py` already does — a shared async
        client cannot be handed to a separate browser process.
        """
