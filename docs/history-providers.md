# History providers

`price-tracker-bot` supports a second kind of drop-in plugin alongside
scrapers: a **history provider**, which answers a different question than a
scraper does. A scraper reads *what a product costs right now*; a history
provider answers *what it cost before this bot started watching*.

Without any installed, a product is added exactly as it is today — the chart
starts empty, `lowest_price`/`highest_price` start at the first read, and the
`all_time_low` alert has nothing to compare against until a few checks have
run. A history provider fills that in once, at add time.

The public repo ships the seam only: the base class, the registry, the
database columns, the chart's two-colour rendering, and the `all_time_low`
alert type. It ships no providers. Two real ones — reading Amazon's price
history via Keepa, and a Spanish price-comparison service — live in a private,
operator-only repository, because they depend on reverse-engineering a
third-party site's internal API rather than reading a published page.

## Contract

Every provider subclasses `AbstractHistoryProvider` from
`price_tracker.core.history_base` and provides:

- `name: ClassVar[str]` — unique stable identifier, same rule as a scraper's:
  the base class default is `""`, and two providers left at the default
  collide.
- `priority: ClassVar[int]` — resolution order. `resolve_all(url)` returns
  every provider willing to try a URL in priority order, and the backfill
  walks that list, stopping at the first that returns usable points.
- `def can_handle(self, url: str) -> bool` — required. Be as narrow as the
  provider actually is: a provider that answers `True` for everything gets
  asked about everything, launching a browser (if that is how it works) for
  URLs it was never going to find anything for.
- `async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult`
  — must never raise. A miss, a timeout, a site that changed shape: all of
  these are `HistoryResult(error="...")`, not an exception. The caller
  (`bot.handlers._history_backfill.backfill_history`) wraps every call
  defensively regardless — a plugin is edited on the operator's host, outside
  review, and a broken one must cost a backfill, never an add — but a provider
  that already promises not to raise is one fewer thing to get wrong.

```python
@dataclass(frozen=True)
class HistoryPoint:
    observed_at: datetime  # timezone-aware, UTC
    price: Decimal


@dataclass(frozen=True)
class HistoryResult:
    points: tuple[HistoryPoint, ...] = ()
    currency: str | None = None
    error: str | None = None
```

`currency` matters only when it differs from the product's own tracked
currency — the caller's only conversion path is to EUR
(`core.currency.convert_to_eur`), because every scraper in this repo reads a
European shop. A provider reporting a currency this deployment cannot convert
to is a clean miss, not a wrong number mixed into the chart's price axis.

## What the caller does with the result

`backfill_history` (`bot/handlers/_history_backfill.py`) runs once, right
after `_add_product` inserts the new row:

1. Walks `history_registry.resolve_all(url)` in priority order.
2. Calls `fetch`, bounded to 45 seconds (`asyncio.wait_for`) and wrapped so a
   raise is treated as a miss.
3. Drops points at or after the moment the product was added — a provider's
   "today" entry would otherwise double up with the bot's own first read —
   and anything priced at zero or below, which every provider seems to use as
   its own way of marking "unavailable that day".
4. Converts currency if needed, or drops the batch if it cannot.
5. Persists what survives with `Repository.add_price_history_bulk`, which
   tags every row `source=<provider name>` (`NULL` means the bot's own
   check), folds the batch's low and high into `products.lowest_price` /
   `highest_price`, and records `history_source` / `history_backfilled_at`.
6. Stops at the first provider that wrote something. A miss falls through to
   the next provider in priority order; a genuine miss from all of them
   leaves the product exactly as it is today — no backfill, no error shown.

The confirmation card gains a fact block (count, first date, source, the
imported floor) and three buttons when something was written: alert at the
imported floor, alert on every drop, or pick a target — the first is new
(`bfmin_target_<id>`), the other two are the buttons those flows already use.

## What it changes on the product

- `products.history_source` / `history_backfilled_at` — set once, at backfill
  time, read by the chart to decide whether to open a wider window
  (`CHART_MAX_WINDOW_DAYS = 730` instead of the usual 90) and draw a dashed
  line at `products.created_at` marking where the import ends.
- `price_history.source` — `NULL` for a live check, the provider's name for
  an imported row. The chart reads this per point to draw imported history in
  a muted grey and the bot's own reads in the usual accent colour.
- `products.lowest_price` / `highest_price` — folded once from the batch, so
  an imported floor is immediately available to the `all_time_low` trigger
  rather than waiting for a live read to happen to beat it.

`initial_price` is untouched: it stays "what it cost when the link was
pasted", which is a different fact than "the cheapest it has ever been" and
worth keeping separately.

## Minimal example

```python
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import httpx

from price_tracker.core.history_base import AbstractHistoryProvider, HistoryPoint, HistoryResult


class MyHistoryProvider(AbstractHistoryProvider):
    name: ClassVar[str] = "myhistory"
    priority: ClassVar[int] = 50

    def can_handle(self, url: str) -> bool:
        return "myshop.com" in url

    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult:
        try:
            response = await client.get(f"https://api.myshop.com/history?url={url}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return HistoryResult(error=f"HTTP error: {exc}")

        data = response.json()
        points = tuple(
            HistoryPoint(
                observed_at=datetime.fromisoformat(row["date"]).replace(tzinfo=UTC),
                price=Decimal(str(row["price"])),
            )
            for row in data.get("history", [])
        )
        if not points:
            return HistoryResult(error="no history returned")
        return HistoryResult(points=points, currency="EUR")
```

A provider that drives a browser rather than calling a JSON API ignores
`client` entirely and launches its own — the same way
`scrapers/playwright_fallback.py` does, soft-importing `playwright` so the
module stays importable on a host without Chromium. `AbstractHistoryProvider`
carries no assumption either way.

## Drop-in installation

Save the file in `plugins/` alongside any drop-in scrapers. One discovery
pass (`core.registry.discover_dropin_plugins`) scans every `*.py` file that
does not start with `_` and registers whichever base class it finds —
`AbstractScraper`, `AbstractHistoryProvider`, or both, if a file defines
both. Restart the bot to pick up new or changed files.

```
plugins/
├── README.md          # tracked
├── myhistory.py        # gitignored, auto-loaded — a history provider
├── myshop.py           # gitignored, auto-loaded — a scraper
└── _shared_helper.py   # gitignored, NOT auto-loaded (leading underscore)
```

An import error in one file is logged and the file is skipped; it does not
stop the others from loading or the bot from starting — the directory is
operator content, edited on the running host outside any review.

## Testing

No network in a provider's own tests: record a fixture (an HTML page, a JSON
response, or a browser XHR capture) once and assert against it offline.

```python
import httpx
import pytest
import respx

from price_tracker.core.history_base import HistoryResult

from myhistory import MyHistoryProvider


@pytest.mark.asyncio
async def test_myhistory_parses_a_recorded_response():
    fixture = '{"history": [{"date": "2026-01-01T00:00:00", "price": "39.99"}]}'

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.myshop.com/history").mock(
            return_value=httpx.Response(200, text=fixture)
        )
        async with httpx.AsyncClient() as client:
            result = await MyHistoryProvider().fetch("https://myshop.com/p/1", client)

    assert result.error is None
    assert len(result.points) == 1
    assert result.points[0].price == pytest.approx(39.99)
```

## Best practices

- **Never raise from `fetch`** — return `HistoryResult(error=...)`. The caller
  already wraps every call defensively, but a provider that keeps its own
  promise is one fewer thing that can go wrong at 3am on someone else's host.
- **Be narrow in `can_handle`** — a provider that answers `True` for every
  URL gets asked about every product, which matters more here than for a
  scraper if answering means launching a browser.
- **Timestamps are UTC and aware** — `HistoryPoint.observed_at` must carry
  `tzinfo`; the caller compares it against the moment the product was added
  and a naive datetime cannot be compared against an aware one.
- **Currency only if it actually differs** — omit it (leave `currency=None`)
  when the provider already reports in the product's own tracked currency;
  setting it to anything but `"EUR"` when the product isn't EUR-tracked drops
  the whole batch, on purpose (see "What the caller does with the result").
- **Do not cache state on the instance** — providers are singletons, same
  rule as scrapers.

## Related docs

- [plugins.md](plugins.md) — the sibling seam for reading a product's
  *current* price, same drop-in mechanism, different base class.
- [scrapers.md](scrapers.md) — built-in scraper inventory.
- [architecture.md](architecture.md) — where this fits in the data flow.
