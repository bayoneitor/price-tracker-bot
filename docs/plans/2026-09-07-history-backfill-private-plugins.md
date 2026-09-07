# Plan — Price history backfill from private plugins

- Date: 2026-09-07
- Status: **in progress.** Checklist below tracks each step as it lands.
- Repo: fork `bayoneitor/price-tracker-bot`, branch `integration`

Prose in English, like the rest of `docs/`. Written so an executor who reads only
this file can carry it out.

---

## Goal

When a product is added, the bot can import past prices so the chart, the min/max,
and a new all-time-low alert have something to work with from day one. The public
repo ships the seam, the schema, the chart, and the alert. The two page scrapers
live in a private GitHub repo cloned into `plugins/`.

Without plugins, adding a product behaves as it does today.

---

## Decisions

- History is a pluggable module, same shape as scrapers: `can_handle` + `fetch`,
  register by priority, first provider that returns points wins.
- A failed or empty backfill never blocks adding a product. Providers return
  `HistoryResult(error=...)` and never raise. Each `fetch` is capped at 45 seconds.
- `initial_price` stays "what it cost when you pasted the link". Imported points
  do update `lowest_price` / `highest_price` once, at backfill time.
- `plugins/` stays gitignored except `README.md`. Compose already bind-mounts
  `./plugins:/app/plugins:ro`. The private repo is cloned into that folder on this
  host. No submodule, no remote, no mention of the private repo in the public tree.
- Private repo: `bayoneitor/price-tracker-plugins`, created with `gh`, owner
  `bayoneitor`.
- Two providers, both Playwright, both intercept the XHR the page itself fires:
  - `keepa` (priority 100) — Amazon URLs only.
  - `precioreal` (priority 50) — allowlist of ES stores (Amazon, PcComponentes,
    MediaMarkt ES, El Corte Inglés, Carrefour, Fnac, Worten).
- Keepa's free graph PNG (`graph.keepa.com/pricehistory.png`) is a separate public
  button. It is an image, it feeds nothing, it needs no plugin.

---

## Public seam

`src/price_tracker/core/history_base.py`:

```python
@dataclass(frozen=True)
class HistoryPoint:
    observed_at: datetime  # timezone-aware UTC
    price: Decimal


@dataclass(frozen=True)
class HistoryResult:
    points: tuple[HistoryPoint, ...] = ()
    currency: str | None = None
    error: str | None = None


class AbstractHistoryProvider(ABC):
    name: ClassVar[str] = ""
    priority: ClassVar[int] = 0

    def can_handle(self, url: str) -> bool: ...
    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult: ...
```

`HistoryRegistry` in `core/registry.py`: `register` / `list` / `resolve_all(url)`.
Unify drop-in discovery into
`discover_dropin_plugins(scraper_registry, history_registry, plugin_dir)` so one
`plugins/*.py` pass registers `AbstractScraper` and `AbstractHistoryProvider`.
Skip `_*.py`. Import errors are logged and skipped.

`src/price_tracker/history/` exists as a package with no built-in providers. Tests
register fakes.

`fetch` receives the shared `httpx.AsyncClient`. Playwright providers ignore it
and launch Chromium themselves, the same way `playwright_fallback.py` already
does. Chromium is in the image; `/tmp` and `~/.cache` are tmpfs.

Wire the registry in `main.py` next to `ScraperRegistry`, stash it on `bot_data`.

---

## Schema — migration 020

(Written as 018 when this plan was drafted; 018 and 019 were since taken by the notification-language and archived-product work. Renumbered here to 020, the next free slot.)

```sql
ALTER TABLE price_history ADD COLUMN source TEXT;   -- NULL = the bot's own read
ALTER TABLE products ADD COLUMN history_source TEXT;
ALTER TABLE products ADD COLUMN history_backfilled_at TEXT;
```

- `add_price_history` cannot set `checked_at` today. Add
  `add_price_history_bulk(product_id, points, *, source)` with `executemany` in
  one transaction.
- `_PRODUCT_COLS`, `_PRODUCT_COLS_P`, `_row_to_product`, and `ProductRecord` move
  together. Append the new columns at the end. `PriceHistoryRecord` gains
  `source: str | None`.
- Folding imported min into `lowest_price` makes the anti-flap rule stricter (a
  new-low must beat the imported floor). Comment that.

---

## Backfill on add

In `_add_product` after `db.add_product` and before the confirmation card:

1. Walk `history_registry.resolve_all(url)`.
2. `asyncio.wait_for(provider.fetch(...), 45)` for each.
3. Drop points with price ≤ 0 or timestamp ≥ `created_at`. Convert currency if it
   differs (`core/currency.py`).
4. Persist with `source=provider.name`, set `history_source` /
   `history_backfilled_at`, fold min/max into `lowest_price` / `highest_price`.
5. Miss or error: continue silently.

The existing "🔍 Analysing the product..." message covers the wait.

When backfill returned data, the confirmation card gains a fact block (count,
first date, source) and three buttons on machinery that already exists:

- alert at the imported min as `threshold_type="target"`
- `any_drop` at today's price
- the existing `settarget_<id>` prompt

Plus the new all-time-low button.

---

## Chart

- Imported points in `INK_MUTED`, the bot's own reads in `SINGLE_SERIES`.
- Dashed `axvline` at `products.created_at` only when imported history exists.
- `CHART_WINDOW_DAYS = 90` stays the default. When `history_source` is set, open
  the window up to `CHART_MAX_WINDOW_DAYS = 730`.
- `get_price_change_points` caps at 500 rows; raise it or let the caller pass the
  limit.

---

## Alerts — `all_time_low`

Fifth `ThresholdType`. It cannot live in `crosses_threshold` (that only sees
`old` and `new`). Evaluate in `Scheduler._check_product_core` **before**
`update_price` rewrites `lowest_price`: fire when `info.price < p.lowest_price`.
A test must pin that order.

Own formatter: "this is the cheapest it has ever been". Button in
`build_threshold_keyboard` and the twin keyboard in `callbacks/_actions.py`;
`track_atl_` branch in `callbacks/_product.py`.

---

## Keepa PNG button

`keepa_<id>` on the product screen sends

```
https://graph.keepa.com/pricehistory.png?asin=<ASIN>&domain=<code>&range=365
```

as a photo, crediting Keepa in the caption. Amazon only. Needs an ASIN +
TLD→domain-code helper; put it in `core/url_utils.py` so the public button and
the private Keepa plugin can both use it.

---

## Private repo (host only)

```bash
gh repo create bayoneitor/price-tracker-plugins \
  --private \
  --description "Operator-only history providers for price-tracker-bot." \
  --disable-wiki \
  --disable-issues
```

Install on this host:

```bash
rm -rf plugins
git clone git@github.com:bayoneitor/price-tracker-plugins.git plugins
git checkout -- plugins/README.md
```

Nested `.git` is ignored by `plugins/*`. Compose does not change. Restart the
container to load new files. Do not `git add` this folder. Do not submodule it.

Layout inside the private repo:

```
keepa.py          # AbstractHistoryProvider, name="keepa"
precioreal.py     # AbstractHistoryProvider, name="precioreal"
_browser.py       # shared Playwright helper (underscore = not auto-loaded)
tests/            # recorded XHR fixtures, no live network
README.md         # operator notes
```

---

## i18n and docs

User-facing strings are English msgids inside `_()` / `N_()`, interpolated with
`str.format`. Then `scripts/i18n.sh` and translate `es_ES` / `it_IT`.
`scripts/audit_english.sh` runs in CI.

Public docs: `docs/history-providers.md` (contract + a trivial fake example),
pointer from `docs/plugins.md`, plus `docs/architecture.md`, `docs/scrapers.md`,
`README.md`, `CHANGELOG.md`. Public docs do not name the private repo or the two
scrapers.

---

## Files

**New (public)**

- `src/price_tracker/core/history_base.py`
- `src/price_tracker/history/__init__.py`
- `src/price_tracker/db/migrations/020_add_history_provenance.sql`
- `docs/history-providers.md`
- `tests/unit/test_history_registry.py`
- `tests/unit/test_history_backfill.py`
- `tests/unit/test_all_time_low.py`

**Modified (public)**

- `core/registry.py`, `core/alert.py`, `core/scheduler.py`, `core/url_utils.py`
- `db/repository.py`, `db/models.py`
- `bot/handlers/product.py`, `bot/keyboards.py`,
  `bot/handlers/callbacks/_actions.py`, `bot/handlers/callbacks/_product.py`,
  `bot/charts.py`
- `main.py`
- `docs/plugins.md`, `docs/scrapers.md`, `docs/architecture.md`, `README.md`,
  `CHANGELOG.md`

**Private repo only**

- `keepa.py`, `precioreal.py`, `_browser.py`, fixtures, tests

---

## Steps

- [x] **1.** **Seam.** `history_base`, `HistoryRegistry`, unified drop-in discovery,
   `main.py` wiring. Unit tests with a fake provider in a tmp `plugins/` dir.

  `HistoryRegistry.list_providers()`, not `.list()` like its `ScraperRegistry`
  twin — reproduced a genuine mypy (strict, 3.12) quirk where a second
  same-named method across classes in one module misresolves its own return
  annotation to itself. Noted in a comment on the method.

- [ ] **2.** **Migration 020 + repository.** Bulk insert, new columns, `ProductRecord` /
   `_PRODUCT_COLS` in lockstep. Migration test against a copy of the schema.

- [ ] **3.** **Backfill on add.** Walk `resolve_all`, filter, persist, silent on miss.
   Tests inject a fake registry; no network.

- [ ] **4.** **Chart.** Two segments, tracking-start line, 730-day window when imported,
   row-cap fix. Extend `tests/unit/test_chart_window.py`.

- [ ] **5.** **`all_time_low` + confirmation-card buttons.** Pin "evaluate before
   `update_price`".

- [ ] **6.** **Keepa PNG button** and the public ASIN helper.

- [ ] **7.** **i18n + public docs.**

- [ ] **8.** **Verify the public side.** `pytest --cov-fail-under=90`, `ruff check`,
   `ruff format --check`, `mypy --strict`, `bash scripts/audit_english.sh`.
   Migration 020 on a copy of `/data/pricetracker.db`. Add product with empty
   `plugins/` is unchanged. Fake plugin registers, wins/loses on priority,
   backfill writes `source`.

- [ ] **9.** **Create the private repo** with `gh` and clone it into `plugins/`. Stubs
   that register and return `HistoryResult()` so startup logs show both names.

- [ ] **10.** **Shared `_browser.py`.** Headless Chromium, same UA as
  `playwright_fallback.py`, `page.on("response")` collector, timeout,
  always-close.

- [ ] **11.** **Keepa scraper.** `can_handle` Amazon + extractable ASIN. Playwright opens
  `https://keepa.com/#!product/{domain}-{ASIN}`, intercepts `/ajax/` JSON,
  decodes `csv[0]` (fallback `csv[1]`): pairs `[keepaMinutes, cents]`, drop
  `-1`, UTC seconds = `(keepaMinutes + 21564000) * 60`,
  `Decimal(cents) / 100`. Soft-import Playwright. Private tests against a
  recorded XHR fixture.

- [ ] **12.** **PrecioReal scraper.** `can_handle` the ES-store allowlist only (never
  every URL). Playwright opens PrecioReal, lets the SPA bootstrap the
  session, intercepts
  `GET /api/proxy?endpoint=get_offer&country=es&url=...`, maps the history
  array to `HistoryPoint`. Soft-import Playwright. Private tests against a
  recorded `get_offer` fixture.

- [ ] **13.** **Verify the scrapers on this host.** Restart compose. Add an `amazon.es`
  product (Keepa should hit; card shows imported count; `/history` draws grey
  then orange with the tracking-start line). Add a PcComponentes product
  (Keepa skips, PrecioReal hits or silent miss). `all_time_low`: set
  `lowest_price` by hand, force a read below it, fires once. Keepa PNG
  button on a real ASIN. Broken plugin file: bot still starts.

---

## Risks

- Both scrapers depend on third-party SPAs. Intercept XHR rather than parse
  canvas/SVG. A live miss is a silent skip, not a failed add.
- Playwright costs ~seconds per add. 45s budget. PrecioReal allowlist so we do
  not launch Chromium for every shop.
- Nested git repo in `plugins/` is fine because `plugins/*` is gitignored. Do
  not `git add` it.
- Positional `_row_to_product`: new columns must be appended at the end of
  `_PRODUCT_COLS` and `ProductRecord` together.

---

## Out of scope

- A paid Keepa API provider.
- Pip entry-point loading for history providers.
- Expanding PrecioReal beyond the initial allowlist.
