# Plan — Imported price history, provenance on the chart, and alerts that use it

- Date: 2026-09-07
- Status: **proposed, not started.** Nothing here is implemented.
- Repo: fork `bayoneitor/price-tracker-bot`, branch `integration`
- Supersedes: `docs/roadmap.md` item 1 ("All-time-low alerts"), which this plan absorbs

Prose in English, like the rest of `docs/`. Written so an executor who reads only
this file can carry it out.

---

## 0. The problem

When you add a product the bot starts from zero. The chart is empty until the
third or fourth check, `initial_price` means only "what it cost when you pasted
the link", and every alert trigger measures against that number. The question
that actually decides a purchase — *is this the cheapest it has ever been?* — the
bot cannot answer, because it has no past.

Three things follow from fixing that:

1. A **history-provider seam**, shaped like the scraper registry that already
   exists, so "where does this product's past come from" is a pluggable module
   and new trackers can be added without touching the core.
2. A chart that distinguishes **imported data from data the bot measured
   itself**, with a mark on the day tracking started.
3. An **all-time-low trigger**, and a threshold choice offered at add time with
   the history in front of you.

Item 3 is worth having on its own: it works on the history the bot accumulates
by itself, with no provider configured at all.

---

## 1. What was verified about external sources (2026-09-07)

This is the perishable part of the research and it constrains the whole scope.
Recorded here so nobody re-derives it.

| Source | Permitted | Returns data points | Free |
|---|---|---|---|
| Keepa official API | Yes, with your own key | Yes, full history | No — from ~€49/month, no free tier |
| `graph.keepa.com/pricehistory.png` | Yes — `keepa.com/robots.txt` disallows only `/r/`, `/ajax/`, `/refererControlDisqus.html`. Probed: HTTP 200, PNG 500×200 | No, it is an image | Yes, no key |
| Keepa website, scraped | **No** — hash-route SPA, data comes from `/ajax/`, which their robots.txt disallows | — | — |
| Wayback Machine CDX | Yes, public documented API | **No, in practice.** Probed: the MSI Shopify URL → `[]`; a PcComponentes product → `[]`; `amazon.com/dp/B08N5WRWNW` → 5 captures in two years, mostly 301/404/500 and one 2 KB bot-block page | Yes |
| `precioreal.com/api/proxy?endpoint=get_offer&country=es&url=…` | **No** — returns `401 Missing Auth Token` (session-token gated); their `robots.txt` has `Disallow: /api/` for `User-agent: *`; their `llms.txt` states no API is offered | Yes | Yes |
| precioreal public offer page | Yes — `/oferta/` is allowed and explicitly citable | No — the server HTML carries only the *current* price in meta tags and JSON-LD; the history is drawn client-side | Yes |

**Conclusion: the only permitted source that returns real data points is Keepa's
paid API, and Keepa covers Amazon only — on the web as much as through the API,
since all their data is the Amazon marketplace.**

That is why this plan ships the seam plus Keepa as the reference implementation,
and leaves any provider that works against a third party's stated policy in
`plugins/` (gitignored) as an operator decision rather than a project one. It is
the line `docs/roadmap.md` already draws under *Deliberately not proposed:
scraping behind logins or paywalls*.

### How the precioreal endpoint was found

Kept because it took a while and someone will ask.

`precioreal.com` is a Vite SPA. The main bundle
(`/assets/index-<hash>.js`) wraps every call in a helper, and the API base is
`` J = `${gn}/api/proxy` ``. Calls look like:

```
ee(`${J}?endpoint=get_offer&country=es&url=${encodeURIComponent(e)}`)
ee(`${J}?endpoint=check_duplicate&url=…`)
ee(`${J}?endpoint=create_alert`)
ee(`${J}?endpoint=list_alerts`)
```

`get_offer` is the search-by-link call. The wrapper first awaits a session
bootstrap and attaches an `X-Session-Token` header; without it the endpoint
answers `401 Missing Auth Token`. So reaching it means either replaying that
bootstrap or rendering the SPA and letting the page make the call itself.

---

## 2. The seam — `AbstractHistoryProvider`

New `src/price_tracker/core/history_base.py`, shaped like `AbstractScraper`
(`core/scraper_base.py:388`):

```python
@dataclass(frozen=True)
class HistoryPoint:
    observed_at: datetime  # timezone-aware UTC
    price: Decimal  # never float


@dataclass(frozen=True)
class HistoryResult:
    points: tuple[HistoryPoint, ...] = ()
    currency: str | None = None
    error: str | None = None


class AbstractHistoryProvider(ABC):
    name: ClassVar[str] = ""
    priority: ClassVar[int] = 0

    def can_handle(self, url: str) -> bool: ...  # abstract
    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult: ...  # abstract
```

Same error contract as scrapers, and it matters more here: **return
`HistoryResult(error=…)`, never raise.** A backfill that fails must never stop a
product from being added. It is a bonus, not a requirement. Unlike scrapers there
is no `BlockEvent` / `ListingGone` distinction to preserve — a history provider
has no per-domain quarantine to feed, and a failure is simply an absence.

---

## 3. The registry

In `src/price_tracker/core/registry.py`, beside `ScraperRegistry`:

- `HistoryRegistry` with `register` / `list` / `resolve_all(url)`.
  Note **`resolve_all`, not `resolve`**: the requirement is "try Keepa first, then
  the next one", so the caller walks the priority-ordered list and keeps the first
  provider that returns points. A provider that returns an empty result is not a
  failure, it is a miss, and the chain continues.
- `discover_builtin_history_providers(registry)` — `pkgutil` scan of the new
  `price_tracker.history` package, mirroring `discover_builtin_scrapers`.
- Drop-in discovery is **unified**. `discover_dropin_scrapers` already
  `exec_module`s every `plugins/*.py`; generalise it to
  `discover_dropin_plugins(scraper_registry, history_registry, plugin_dir)` which
  registers `AbstractScraper` subclasses **and** `AbstractHistoryProvider`
  subclasses in the same pass. One `plugins/` directory, two registries, one
  restart. This is the extension point the whole feature is for.

Wire it in `src/price_tracker/main.py` next to the existing `ScraperRegistry`.

---

## 4. Built-in provider: Keepa (official API)

`src/price_tracker/history/keepa.py`, `name = "keepa"`, `priority = 100`.

- `can_handle`: an Amazon URL **and** `config.keepa_api_key` is set. With no key
  it returns `False` and the provider simply does not take part — the same quiet
  degradation `playwright_fallback.py` uses for a missing dependency.
- New config field in `src/price_tracker/config.py`: `keepa_api_key: str = ""`
  from `os.getenv("KEEPA_API_KEY", "")`. Add it to `.env.example`.
- ASIN extraction and TLD → Keepa `domain` mapping (com=1, co.uk=2, de=3, fr=4,
  co.jp=5, ca=6, it=8, es=9, …). **`scrapers/amazon.py` does not expose a reusable
  ASIN extractor today** — write one in the provider, or lift it into
  `core/url_utils.py` if it comes out clean.
- `GET https://api.keepa.com/product?key=…&domain=…&asin=…&history=1`. One token
  per product.
- Decoding: `csv[0]` (Amazon price), falling back to `csv[1]` (third-party new).
  Flat array of `[keepaMinutes, cents]` pairs; `-1` means out of stock and is
  dropped; UTC seconds = `(keepaMinutes + 21564000) * 60`; price =
  `Decimal(cents) / 100`. The `21564000` offset is confirmed against
  `keepacom/api_backend`, `KeepaTime.java`.

> **Verify against `keepa.com/api-docs/` when implementing.** The exact domain
> codes and `csv` indices could not be read from the official docs (it answers 403
> to automated requests); they come from secondary sources.

Tests use a recorded JSON fixture and no network, matching
`tests/unit/scrapers/`.

---

## 5. Schema — migration 018

`src/price_tracker/db/migrations/018_add_history_provenance.sql`, opening with a
comment that explains the why, like every other non-trivial migration:

```sql
ALTER TABLE price_history ADD COLUMN source TEXT;   -- NULL = the bot's own read
ALTER TABLE products ADD COLUMN history_source TEXT;
ALTER TABLE products ADD COLUMN history_backfilled_at TEXT;
```

Semantics, decided:

- **`initial_price` is left alone.** It keeps meaning "what it cost when you
  started tracking", which is exactly the anchor the chart marks. Rewriting it
  with an imported first price would destroy the only record of when *you*
  arrived.
- **`lowest_price` / `highest_price` do absorb the imported extremes**, once, at
  backfill time. "📉 Min" on the product card then means the product's real floor
  rather than the floor of the three reads so far. Deliberate side effect: the
  `_is_duplicate_alert` anti-flap rule, which lets an alert through when it is a
  new low, becomes *stricter* rather than looser. Say so in a comment.

In `db/repository.py`:

- `add_price_history` takes no `checked_at` (`repository.py:504`) and the backfill
  must insert with explicit timestamps. Add
  `add_price_history_bulk(product_id, points, *, source)` using `executemany` in a
  single transaction.
- `_PRODUCT_COLS` / `_PRODUCT_COLS_P` and `_row_to_product` hydrate by positional
  index — all three move together, plus `ProductRecord` in `db/models.py`.

---

## 6. Backfill on add

In `_add_product` (`bot/handlers/product.py:255`), after `db.add_product` and
**before** the confirmation card is rendered:

1. `for provider in history_registry.resolve_all(url):` → `fetch`; first one with
   points wins.
2. Drop points with price ≤ 0 or timestamps at or after `created_at` — the present
   is measured by the bot, not asserted by a provider — and convert the currency
   if it differs (`core/currency.py`).
3. `add_price_history_bulk(..., source=provider.name)`, set `history_source` and
   `history_backfilled_at`, fold the imported min/max into `lowest_price` /
   `highest_price`.
4. No provider, or no points: carry on exactly as today. Silent.

The `🔍 Analysing the product...` message is already on screen and is edited at
the end, so the backfill fits inside that same wait with no new UI.

---

## 7. The chart

`src/price_tracker/bot/charts.py`:

- **Tracking-start mark.** An `axvline` at `products.created_at`, dashed, in
  `INK_MUTED`, with a short label. Drawn **only when imported history exists** — on
  a product with no backfill the line would sit on the left edge and say nothing.
- **Two segments.** Imported points in `INK_MUTED`, the bot's own reads in
  `SINGLE_SERIES` (`#ff9f1c`). One glance separates borrowed data from measured
  data, which is the whole point of recording provenance.
- **Window.** `CHART_WINDOW_DAYS = 90` (`charts.py:71`) would crop two years of
  imported history. `generate_chart` (`charts.py:212`) should ask for the full span
  of the imported history when `history_source` is not NULL, under a new
  `CHART_MAX_WINDOW_DAYS = 730`. `chart_window()` stays the default for everything
  else. `tests/unit/test_chart_window.py` covers this and needs extending.
- **Row cap.** `get_price_change_points` (`repository.py:538`) collapses unchanged
  runs with `LAG`/`ROW_NUMBER` but caps at `limit_per_product = 500`. Two years of
  imported changes can exceed that. Raise it, or let the caller pass it.

---

## 8. Alerts

### 8a. New `all_time_low` trigger

This is `docs/roadmap.md` item 1. With imported history behind it, it finally
means what it says.

- Fifth value in `ThresholdType` (`core/alert.py:29`).
- `crosses_threshold` cannot host it: it only receives `old` and `new`, and this
  trigger needs `lowest_price`. Evaluate it in `Scheduler._check_product_core`
  (~line 800) beside `target_hit`, which is already a crossing rather than a
  state. It fires when `info.price < p.lowest_price` — **before** `update_price`
  rewrites `lowest_price`. The ordering of those two calls is the whole
  correctness of this feature; a test must pin it.
- Its own formatter in `core/alert.py`. The sentence that matters is "this is the
  cheapest it has ever been", not the percentage drop.
- A button in `build_threshold_keyboard` (`bot/keyboards.py:112`) and in the twin
  keyboard at `callbacks/_actions.py:62`; a `track_atl_` branch in
  `callbacks/_product.py:274` alongside the four that exist.

### 8b. Choosing a threshold with the history in view

When the backfill returned data, the confirmation card gains a block:

```
📊 412 prices since 2024-03-11 (keepa)
All-time low: €219.00 · Today: €259.00
```

and three buttons, all on machinery that already exists — no new threshold types
beyond 8a:

| Button | Effect |
|---|---|
| `🎯 Alert me at €219.00 (all-time low)` | `set_target_price(min)` + `threshold_type="target"` |
| `🔔 Alert me below €259.00 (today)` | `threshold_type="any_drop"` |
| `✏️ A different price` | the existing `settarget_<id>` prompt |

Plus `📉 New all-time low` from 8a.

Keep it descriptive. `docs/roadmap.md` is right that a price tracker which starts
guessing loses the trust that makes it useful — these buttons state facts and set
thresholds, they do not advise.

---

## 9. Keepa's free graph image (value with no key, Amazon only)

Independent of the seam and of the backfill, and the only thing in this document
that delivers Amazon history on day one without paying: a `keepa_<id>` button on
the product screen that sends

```
https://graph.keepa.com/pricehistory.png?asin=<ASIN>&domain=<code>&range=365
```

as a photo, crediting Keepa in the caption. No key, public embed endpoint,
allowed by their robots.txt, verified returning a PNG. It is an image, so it
feeds nothing — not the chart, not `lowest_price`, not an alert. That limit is the
reason it is a separate feature and not a `HistoryProvider`.

---

## 10. Docs

- New `docs/history-providers.md`: the contract, a complete minimal example, and
  how `plugins/` discovery finds it. Mirror of `docs/plugins.md`.
- `docs/plugins.md`: point at it from the discovery section.
- `docs/scrapers.md`, `docs/architecture.md`, `README.md`, `CHANGELOG.md`.
- `docs/roadmap.md`: mark item 1 as absorbed here.
- Copy §1 of this file into `docs/history-providers.md` as an honesty section, so
  the next person does not re-run the same probes.

---

## 11. Deliberately not in the repo

**`plugins/precioreal.py`** — a provider that renders precioreal's SPA with
Playwright so the page itself makes the authenticated `/api/proxy` call. It goes
in `plugins/` (gitignored) if the operator wants it. The doc records that their
robots.txt disallows `/api/` and their llms.txt states no API is offered. This is
a self-hosted bot and that is the operator's call; what the project does not do is
distribute it.

**A key-less scraped Keepa provider is not documented as an option at all.** Its
data comes from `/ajax/`, which their robots.txt disallows, and the permitted free
alternative is a PNG that can only ever be an image (§9). There is no third
position here worth writing down.

---

## 12. Files

**New**

- `src/price_tracker/core/history_base.py`
- `src/price_tracker/history/__init__.py`, `src/price_tracker/history/keepa.py`
- `src/price_tracker/db/migrations/018_add_history_provenance.sql`
- `docs/history-providers.md`
- `tests/unit/test_history_registry.py`, `tests/unit/test_history_backfill.py`,
  `tests/unit/test_all_time_low.py`, `tests/unit/history/test_keepa.py`,
  `tests/fixtures/keepa/product.json`

**Modified**

- `core/registry.py` (HistoryRegistry, unified drop-in discovery),
  `core/alert.py` (`all_time_low`), `core/scheduler.py` (evaluation order),
  `config.py` (`keepa_api_key`)
- `db/repository.py` (`add_price_history_bulk`, new columns, `limit_per_product`),
  `db/models.py` (`ProductRecord`)
- `bot/handlers/product.py` (backfill + card), `bot/keyboards.py`,
  `bot/handlers/callbacks/_actions.py`, `bot/handlers/callbacks/_product.py`,
  `bot/charts.py`
- `main.py`, `.env.example`, `docs/*`, `README.md`, `CHANGELOG.md`

---

## 13. Conventions to respect

- `Decimal` for money, never `float` — only at the matplotlib boundary.
- User-facing strings are **English msgids** inside `_()` / `N_()`, interpolated
  with `str.format`, never f-strings (the extractor cannot see those). Then
  `scripts/i18n.sh` to regenerate the `.pot` and translate `es_ES` / `it_IT`.
  `scripts/audit_english.sh` runs in CI.
- Comments, docstrings and commits in English, explaining *why*; Conventional
  Commits with a lowercase declarative subject.
- `mypy --strict`, `ruff`, line length 100, `from __future__ import annotations`.
- Test names as English sentences; a module docstring stating the problem the file
  exists for.

---

## 14. Verification

1. Full `pytest` — the CI gate is `--cov-fail-under=90`.
2. `ruff check`, `ruff format --check`, `mypy --strict`,
   `bash scripts/audit_english.sh`.
3. Migration: run against a copy of `/data/pricetracker.db`, confirm 018 applies
   and existing products still list and still chart.
4. **Without `KEEPA_API_KEY`**: adding a product behaves exactly as today, with no
   new noise; `resolve_all` returns an empty list.
5. **With a key** (or an injected fixture): adding an Amazon product shows the
   history block on the card; `/history` draws the imported segment in grey, the
   vertical line at `created_at`, and the bot's own segment in orange.
6. **`all_time_low`**: set `lowest_price` by hand, force a read below it, confirm
   it fires once and does not repeat on the next sweep.
7. The Keepa image button against a real `amazon.es` ASIN.
8. **The seam itself**: drop a trivial provider in `plugins/`, restart, confirm
   from the logs that it registers in the `HistoryRegistry` and that it wins or
   loses against Keepa according to its priority.
