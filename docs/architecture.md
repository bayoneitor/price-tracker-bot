# Architecture

> Reference: layered architecture, data flow, database schema, and plugin extension point.

## Overview

`price-tracker-bot` is a self-hosted Telegram bot for multi-site product price tracking. The codebase follows a **Core + Plugin** pattern: the public repository ships every site-specific scraper, the core scheduler/alert/health/notifier, the database layer, and the observability stack. The `plugins/` directory is a runtime extension point — drop-in custom scrapers and history providers can live there without forking the repo (see [plugins.md](plugins.md) and [history-providers.md](history-providers.md)). The public/private boundary is intentionally light: only the `.env` (secrets) and `data/pricetracker.db` (user data) stay private; everything else is open.

## Layer diagram

```
src/price_tracker/
├── bot/             # Telegram interface — handlers, decorators, message templates
│   ├── navigation.py  # what the bot is waiting for, and where "back" goes
│   ├── keyboards.py   # shared buttons; every action screen ends with nav_row()
│   ├── charts.py      # single-product and group-comparison renderers
│   ├── commands.py    # the short list published to Telegram's Menu button
│   └── handlers/      # auth, monitoring, settings, product, groups, history, debug, ...
│       └── callbacks/ # per-domain button handlers + _nav (back/close/cancel)
├── core/            # scheduler, alert, outlier, health, currency, retry, http_client
│   ├── history_base.py  # AbstractHistoryProvider seam — backfilling a product's past
│   └── registry.py       # ScraperRegistry + HistoryRegistry, drop-in discovery
├── scrapers/        # 17 site-specific scrapers + generic chain + playwright fallback
├── history/         # empty on purpose — no built-in history providers ship here
├── db/              # repository, models, versioned migrations (001-020)
├── notifier/        # telegram delivery, preferences, digest queue
├── observability/   # Prometheus metrics + structured JSON logging
└── locale/          # gettext catalogs (en, it_IT, es_ES)
plugins/             # extension point for custom scrapers + history providers (gitignored except README.md)
```

Each top-level package has one responsibility and exposes a clear interface to the next layer. Cross-layer calls always flow downward (`bot/` → `core/` → `db/` / `scrapers/` / `notifier/`); `core/` is the orchestrator.

### Two things `bot/` keeps centrally

**What the bot is waiting for.** A screen that asks the user to type something arms a
`PendingInput` (`bot/navigation.py`) recording the action, what kind of answer it expects,
and the message that asked. One text handler reads it: an open prompt wins, then a pasted
link, then a bare number steering an open `/list`. Ordering handler registrations cannot
express "unless something else is pending", which is why a pasted URL used to be tracked
even when the bot had just asked for one.

**Where "back" goes.** The same panel is reachable from several places, so no screen can
name its own parent. The callback dispatcher records, per message, the tokens that rendered
it, and ◀️ Back re-dispatches the previous one — tokens rather than snapshots, so a screen
returned to is rebuilt from current data. `nav_row()` is what puts the button on a screen;
the dispatcher does the bookkeeping so no screen has to.

Rendering is a pure function of its inputs wherever a view is non-trivial
(`product_list.build_list_view`, `groups_view.build_group_view`, `build_comparison_table`),
so the command and the callback build the same panel and tests assert on it without a
Telegram round trip.

## Data flow — scheduler tick to notification

```
1. core.scheduler tick (every CHECK_INTERVAL_MINUTES, default 360)
2. db.repository.list_active_users() → [UserRecord]
   for each user:
     db.repository.list_products_for_user(user_id, only_active=True) → [ProductRecord]
3. for each product:
   a. core.health.HealthManager
      - if is_locked(domain): skip, record skipped_locked metric
      - elif is_half_open(domain): allow one probe only
      - else (open): proceed normally
   b. registry.resolve(url) → AbstractScraper | None  (registry from core.registry)
   c. await scraper.scrape(url, http_client) → ProductInfo
      - tenacity retry with exponential backoff
      - on failure: HealthManager.record_block(domain, reason); continue
      - on success: HealthManager.record_success(domain)
   d. core.outlier.is_outlier(new_price, history) → bool
      - reject if median ratio outside acceptable band
   e. db.repository.add_price_history(product_id, price)
   f. core.alert.crosses_threshold(old, new, threshold_type, threshold_value) → bool
      - threshold types: percentage / fixed / target_price
   g. if alert triggered:
      - notifier.preferences.resolve(user_id=..., product_id=...) → EffectivePrefs
        - encapsulates mute, digest_mode, quiet_hours, throttle, timezone
      - if EffectivePrefs allows immediate send: deps.notifier(user_id, formatted_text)
        (callable is wired to TelegramNotifier.send_alert)
      - else if digest mode: notifier.digest.enqueue(user_id, alert)
4. observability.metrics records counters/histograms throughout
5. observability.logging emits structured JSON events for every state change
```

## Database schema

SQLite database at `DATABASE_PATH` (default `/data/pricetracker.db`). 8 tables:

| Table                | Purpose                                                    | Migration       |
| -------------------- | ---------------------------------------------------------- | --------------- |
| `users`              | Authorized Telegram users + admin flag + nickname          | 001/003         |
| `products`           | Tracked products: URL, threshold, interval, state, history provenance | 001/002/006/007/020 |
| `price_history`      | Price points (Decimal as TEXT) + currency + ts + source      | 001/020         |
| `bot_config`         | Singleton key/value runtime config                         | 001             |
| `scraper_health`     | Per-domain block count + locked_until timestamp            | 008             |
| `notification_prefs` | Per-user mute, digest, quiet hours, timezone, throttle     | 009             |
| `digest_queue`       | Pending alerts batched for periodic flush                  | 010             |
| `schema_version`     | Migrator-managed table tracking applied versions           | (migrator)      |

Key indices:
- `idx_price_history_product` — fast price history lookup per product
- `idx_products_active` / `idx_products_user` — active-product filters per user
- `idx_scraper_health_locked_until` — quarantine state queries
- `idx_notif_prefs_user` — preference resolution per user
- `idx_digest_pending` — digest queue scan

Migrations are versioned `.sql` files in `src/price_tracker/db/migrations/` (001-020), applied at startup by `db.migrator.apply_migrations()`. The `schema_version` table records the highest applied version.

## Plugin extension point

Custom scrapers can be added without modifying the core repository:

1. **Drop-in directory**: place `plugins/<name>.py` (gitignored except `README.md`). Auto-discovered at startup via `core.registry`.
2. **Pip-installable plugin**: declare an entry point in your package's `pyproject.toml`:
   ```toml
   [project.entry-points."price_tracker.scrapers"]
   mysite = "my_plugin.scraper:MySiteScraper"
   ```
   Auto-discovered via `importlib.metadata.entry_points`.

Both forms must subclass `AbstractScraper` (`core/scraper_base.py:172`) and implement `async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo`. See [plugins.md](plugins.md) for the full contract and a minimal example.

A second, sibling plugin kind lives in the same `plugins/` directory and is discovered in the same pass (`core.registry.discover_dropin_plugins`): a **history provider** subclasses `AbstractHistoryProvider` (`core/history_base.py`) and backfills a product's *past* prices at add time, rather than reading its current one. See [history-providers.md](history-providers.md) for the contract.

## Cross-references

- [scrapers.md](scrapers.md) — built-in scraper inventory.
- [history-providers.md](history-providers.md) — the history-backfill plugin seam.
- [observability.md](observability.md) — metrics catalog + dashboard panels.
- [operations.md](operations.md) — deploy, env vars, backup, troubleshooting.
