<p align="center">
  <img src="docs/img/cover.png" alt="price-tracker-bot — self-hosted Telegram bot for multi-site price tracking" width="100%">
</p>

# price-tracker-bot

[![CI](https://github.com/bernalli/price-tracker-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/bernalli/price-tracker-bot/actions/workflows/ci.yml)
[![Security](https://github.com/bernalli/price-tracker-bot/actions/workflows/security.yml/badge.svg)](https://github.com/bernalli/price-tracker-bot/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-1.0.0-brightgreen.svg)](https://github.com/bernalli/price-tracker-bot/releases)

Self-hosted Telegram bot for multi-site price tracking with auto-quarantine, structured observability, fine-grained notification preferences, and a plugin architecture for adding new sites.

<p align="center">
  <img src="docs/img/price-chart.png" alt="Price-history chart with target line, as sent by the /chart command" width="790">
  <br>
  <em>The <code>/chart</code> command: price history with your target line, rendered by the bot.</em>
</p>

## Why this bot

| Feature                          | price-tracker-bot | Camelcamelcamel | Keepa | Pricepulse |
| -------------------------------- | ----------------- | --------------- | ----- | ---------- |
| Self-host                        | ✅                 | ❌               | ❌     | ❌          |
| Multi-site (17 built-in)         | ✅                 | ❌ (Amazon only) | ❌     | partial    |
| Plugin extension point           | ✅                 | ❌               | ❌     | ❌          |
| Full observability (Prom+Grafana)| ✅                 | ❌               | ❌     | ❌          |
| Fine-grained notifications       | ✅                 | basic           | basic | basic      |
| Open-source (MIT)                | ✅                 | ❌               | ❌     | ❌          |

## Key features

- 17 built-in scrapers (Amazon, eBay, Shopify-generic, Walmart, Target, BestBuy, Etsy, Newegg, Wayfair, MediaMarkt, Otto, Zalando, Apple Store, Google Store, AliExpress, Generic JSON-LD/microdata/OG/RDFa chain, Playwright fallback)
- Per-domain auto-quarantine with tier-based exponential backoff (closes infinite-429 loops)
- Multi-currency price tracking (Decimal precision, ECB rates with persistent TTL cache)
- Outlier detection via median ratio (rejects bogus parses without polluting price history)
- Notification preferences: mute, digest, quiet hours, throttle, timezone-aware, per-product — as commands or as buttons
- Product groups: compare a set of products side by side, chart them together, and see which has been cheapest over time
- Prometheus exporter on `127.0.0.1:9090` + structured JSON logging via structlog
- Grafana dashboard with 14 panels (latency, block rate, quarantine map, alerts, currency)
- Plugin extension point at `plugins/` for custom scrapers
- Trilingual UI (English + Italian + Spanish) with auto-detect from Telegram `language_code`
- Hardened Docker deploy: non-root, read-only root fs, dropped capabilities, no-new-privileges, resource limits

## Quick start

```bash
git clone https://github.com/bernalli/price-tracker-bot.git
cd price-tracker-bot
cp .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN and ALLOWED_USERS
docker compose up -d
docker compose logs -f price-tracker-bot
```

Send `/start` to your bot from Telegram. The first user listed in `ALLOWED_USERS` is auto-promoted to admin.

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in:

| Variable                | Default                  | Description                                                                      |
| ----------------------- | ------------------------ | -------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`    | (required)               | Telegram bot API token                                                           |
| `ALLOWED_USERS`         | (required)               | Comma-separated Telegram user IDs authorized to use the bot (first listed becomes admin) |
| `DATABASE_PATH`         | `/data/pricetracker.db`  | SQLite database path                                                             |
| `LOCALE`                | `en`                     | Default locale fallback when the Telegram `language_code` is missing             |
| `CHECK_INTERVAL_MINUTES`| `360`                    | Global sweep interval                                                            |
| `MAX_CONSECUTIVE_ERRORS`| `10`                     | Failed checks before a product is auto-suspended                                 |
| `LISTING_GONE_CONFIRMATIONS` | `3`                  | Consecutive HTTP 404/410 answers before a removed listing is suspended           |
| `READ_CONFIRMATIONS`    | `3`                      | Agreeing checks required before an implausible price raises an alert             |
| `PROMETHEUS_BIND`       | `127.0.0.1:9090`         | Prometheus exporter bind address (host:port)                                     |
| `LOG_LEVEL`             | `INFO`                   | structlog log level                                                              |

See [docs/operations.md](docs/operations.md) for full operational reference.

## Commands

Every command has an English name and, where it existed first, an Italian alias — both
are registered, so `/list` and `/lista` are the same command.

Telegram's **Menu button** (bottom-left of the chat) lists the most-used commands with
a description, so they can be tapped instead of typed. That list is deliberately short:
Telegram's menu is flat — it has no submenus — so it publishes eight entries rather than
the whole surface. Everything else still works when typed, is reachable as a button under
`/menu`, and is listed under **Menu → Info → All commands**. The list is published on
startup and follows the client's language; admin commands appear only in admins' own chats.

Every screen with an action carries its own way out: **◀️ Back** returns to whatever screen
opened it, **✖ Cancel** drops a question the bot is waiting for, **✖ Close** dismisses the
panel. When the bot asks you to type something, the answer is shown by editing the question
and your typed message is removed, so an exchange stays one message instead of four.

### Tracking
- `/start` — register and view the main menu
- `/menu` — open the inline menu
- `/help` — command reference
- `/add <url>` (`/aggiungi`) — start tracking a product
- `/list` (`/lista`) — one paginated message: an index of every product, the selected product's card (price, store, drop since tracking start), and buttons to page, jump, act on it or close the listing. Typing an index number jumps straight to that product
- `/groups` (`/grupos`, `/gruppi`) — named sets of products compared against each other: a side-by-side table, one chart with a line per member, and which one has been cheapest over time
- `/cancel` (`/cancelar`, `/annulla`) — abandon whatever the bot is waiting for you to type
- `/delete <id>` (`/elimina`) — stop tracking
- `/check <id>` (`/controlla`) — check one product now
- `/checkall` — check every product now
- `/pause <id>` (`/pausa`) / `/reactivate <id>` (`/riattiva`) — suspend and resume checks
- `/history <id>` (`/storia`) — price history chart
- `/reset <id>` (`/azzera`) — rebase the reference price to the current one

### Thresholds and targets
- `/threshold <id> <pct|off>` (`/soglia`) — percentage alert threshold
- `/target <id> <price>` — alert when the price reaches this value
- `/refresh <id> <minutes>` — per-product check interval (`0` returns it to the global one)
- `/setinterval <minutes>` (`/intervallo`) — global check interval (admin)

### Notification preferences (per user)

All of these are also under **Menu → Notifications → Delivery**, with buttons instead of
syntax — which matters, since the usual reason to reach for them is a notification that
has just woken you up.

- `/mute <id|all> [duration]` / `/unmute <id|all>` — silence alerts
- `/digest_mode <on|off>` — batch alerts into a periodic digest
- `/digest_now` — flush the pending digest immediately
- `/quiet_hours <HH:MM-HH:MM>` — silent window (timezone-aware)
- `/timezone <IANA>` — your timezone (e.g. `Europe/Rome`)
- `/throttle <max_per_hour>` — sliding-window rate limit
- `/prefs` — current preferences

### Data
- `/export` (`/esporta`) — CSV export of tracked products
- `/import` (`/importa`) — import products from a CSV file
- `/status` (`/stato`) — bot status and counters
- `/errors` (`/errori`) — recent per-product read failures with the reason

### Admin
- `/adduser <telegram_id>` — authorize a user
- `/removeuser <telegram_id>` — revoke authorization
- `/users` (`/utenti`) — list authorized users
- `/nick <telegram_id> <nickname>` — assign a display nickname
- `/health` — scraper health and quarantine state
- `/debug <url>` — run a scraper against a URL without tracking it

## Groups: comparing products against each other

Tracking three monitors tells you each one's price and nothing about how they stand
against each other. A group answers that.

```
/groups                          → 🏷 Groups (0) — you have none yet
  ➕ New group  →  type: Monitors
/list                            → pick a product → ✏️ Edit → 🏷 Groups → ✅ Monitors
```

Or from the group itself: **`/groups` → Monitors → ➕ Add product**. A product can be in
several groups at once — a monitor belongs in `Monitors` and in `Christmas gifts` without
being tracked twice.

**📊 Compare** — every member side by side, cheapest first:

```
📊 Comparison

#7 LG 27GP850-B — €399.00 (-7.0%)  min €389.00
#3 Dell U2724DE — €479.00 (-12.8%)  min €459.00
#9 BenQ PD2705U — €529.00 (+6.0%)  min €499.00

🥇 Cheapest now: LG 27GP850-B at €399.00
↔️ Spread: €130.00
```

The percentage is the change since you started tracking that product, so a product can be
the cheapest today and still the one that has risen most.

**🕐 Who has been cheapest** — the same group over time, read out of the price history the
bot already had, so a group created today covers every month its members have been tracked:

```
🕐 Who has been cheapest

02/07 — Dell U2724DE at €549.00
03/07 — BenQ PD2705U at €499.00
04/07 — LG 27GP850-B at €429.00
16/07 — LG 27GP850-B at €409.00
27/07 — LG 27GP850-B at €399.00
```

**📈 Chart** draws one line per member on a single axis (prices in other currencies are
converted to euro first, so the lines are actually comparable). Up to eight members are
drawn; past that the group is charted in part rather than with two products sharing a
colour.

Deleting a group deletes only the grouping — the products stay tracked.

## Getting out of a screen

Every screen that does something ends with the same row:

| Button | What it does |
|--------|--------------|
| ◀️ Back | returns to whichever screen opened this one, rebuilt from current data |
| ✖ Cancel | drops the answer the bot is waiting for (also `/cancel`) |
| ✖ Close | dismisses the panel |

So a question the bot asks is never a trap:

```
/target                                        → pick a product, then:

  🎯 Dell U2724DE (current: €479.00)
  Type the target price (e.g. 29.99):          [✖ Cancel]

you: 420                                       ← removed from the chat

  🎯 Target: €420.00                            ← the question itself is rewritten
  📦 Dell U2724DE
  💰 Current: €479.00 (-12.3% needed)          [✖ Close]
```

(◀️ Back joins the row whenever the screen was opened from another one — from `/menu`
or from `/list` — and is left off when there is nowhere to go back to, as above.)

If you meant to track a link instead, the bot says so rather than quietly adding it:

```
you: https://www.example.com/p/1234

  ⏳ I am still waiting for a target price.
  Send /cancel first if you wanted to track that link instead.
```

## Supported sites

See [docs/scrapers.md](docs/scrapers.md) for the full list of 17 built-in scrapers with status, coverage, and notes. Generic fallback (`GenericScraper`) handles any site exposing JSON-LD, microdata, OpenGraph, or RDFa product metadata.

## Observability

Prometheus metrics exposed on `127.0.0.1:9090/metrics` (counter, gauge, histogram for scraper duration, block events, quarantine state, alerts, notifications, currency lookups). Structured JSON logs via structlog. Grafana dashboard at `docs/grafana/price-tracker-dashboard.json` (14 panels). See [docs/observability.md](docs/observability.md).

## Plugin extension

Drop a custom scraper file in `plugins/<name>.py` (gitignored except `README.md`) or install a pip package with the `price_tracker.scrapers` entry-point group. See [docs/plugins.md](docs/plugins.md) for the contract and a minimal example.

## Localization

Three locales shipped: `en` (source language), `it_IT` and `es_ES`. Runtime selection auto-detects from Telegram `language_code`, falls back to the `LOCALE` environment variable, then to `en`. `LOCALE` is the fallback for clients whose language has no catalog — a supported client language always wins over it. To add a translation, see [docs/i18n.md](docs/i18n.md).

## Project structure

```
src/price_tracker/
├── bot/            # Telegram interface (handlers, decorators, messages)
├── core/           # scheduler, alert engine, outlier detection, health, currency
├── scrapers/       # 17 built-in site-specific scrapers + generic chain
├── db/             # SQLite repository, models, versioned migrations
├── notifier/       # delivery, preferences, digest, throttle
├── observability/  # metrics, structured logging
└── locale/         # gettext catalogs (en, it_IT, es_ES)
plugins/            # extension point for custom scrapers
docs/               # user + contributor documentation
tests/              # pytest suite (1069 tests, ≥90% coverage)
```

## Stability

**1.0 means the two things you build habits around are now stable**: the SQLite schema and
the command surface. Migrations from any 1.x to a later 1.x apply forward without data loss,
and a command that exists in 1.0 keeps its name and its arguments for the whole 1.x line.
Removing a command or breaking the schema would be a 2.0, not a 1.x.

What is explicitly *not* covered: the internal Python API (`price_tracker.*` is not a library),
the wording of notification texts, and the scraper set — sites change their markup and scrapers
follow them, which is maintenance rather than a breaking change.

## Roadmap

- v0.1.0 — first public release: GitHub + ghcr.io image
- v0.2.0 — confirmation-based alerting: a single bad scrape can no longer raise a price-drop alert
- **v1.0.0 — stable schema and command surface**; per-domain quarantine reachable from every
  scraper, public metadata and artwork carrying no real tracked listing
- next — operational notices grouped per store and explaining themselves, per-product check
  intervals honoured by the scheduler, full UI localisation

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). All contributions welcome: bug reports, feature suggestions, scraper plugins, translations, dashboard panels.

## License

MIT — see [LICENSE](LICENSE).
