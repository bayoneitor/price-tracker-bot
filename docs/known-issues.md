# Known issues

Defects and risks found in a read of the code on 2026-09-07, with the evidence
that established each one. Ticked as they are fixed, with the commit that did it.

New features are not here — those live in [roadmap.md](roadmap.md).

## Behaviour that lies

- [x] **1. The per-product check interval is never honoured.** ✅ `_is_due` in
  `core/scheduler.py` skips a product until its own interval has passed. The
  interval is a *minimum gap*: it cannot make a product checked more often than
  the sweep runs, so `/refresh` now says so when the number asked for is smaller
  than the sweep, rather than letting it imply something it cannot deliver. `/refresh 3 30`
  writes `products.check_interval_minutes` (`db/repository.py:334`), the listing
  card renders it (`bot/handlers/product_list.py:122`), and the scheduler never
  reads it: `run_check_all` walks every active product on every tick
  (`core/scheduler.py:380`) and `check_interval_minutes` appears nowhere in
  `core/`. The bot reports a setting it does not apply. Already named on the
  README roadmap as "per-product check intervals honoured by the scheduler".

  <details><summary>What it was</summary>

  `/refresh 3 30` wrote `products.check_interval_minutes`
  (`db/repository.py:334`), the listing card rendered it
  (`bot/handlers/product_list.py:122`), and the scheduler never read it:
  `run_check_all` walked every active product on every tick and
  `check_interval_minutes` appeared nowhere in `core/`.
  </details>

- [ ] **2. `/debug` fetches any URL with no SSRF check.** `cmd_debug`
  (`bot/handlers/debug.py`) hands its argument to httpx, then to curl_cffi and
  Scrapling, without `validate_public_url` — the guard `/add` uses and the one
  upstream has just applied to CSV import for this exact reason. Admin-only, but
  the admin is the person most likely to paste an internal URL to see what a
  scraper reads.

- [ ] **3. A redirect walks past the SSRF guard.** `validate_public_url` checks
  the URL it is given; the shared client then follows redirects
  (`core/http_client.py:20`) without re-checking. A public host answering
  `302 → http://169.254.169.254/` is fetched anyway. This affects `/add` and CSV
  import, not only `/debug`.

## Operational risk

- [ ] **4. The whole bot package is excluded from coverage.**
  `pyproject.toml` omits `src/price_tracker/bot/*` — 7,539 lines, the largest
  module and the one under most change. The reported figure describes everything
  *except* the user-facing surface, so a regression there fails no gate.

- [ ] **5. CSV import has no bound.** `download_to_memory` takes the whole file
  (`bot/handlers/product_io.py`) and the loop scrapes every row in turn. Telegram
  caps a bot download at 20 MB, which is still tens of thousands of URLs and as
  many outbound requests.

- [ ] **6. No automated backup.** `docs/operations.md` documents the SQLite
  `.backup` call to run by hand. The database is one named volume; nothing takes
  a copy on a schedule.

## Debt that already bites

- [ ] **7. Two names for one object.** `main.py` publishes the same repository as
  `bot_data["repository"]` (`:49`) and `bot_data["db"]` (`:56`). Both are in use.
  An alias from the monolith that was never retired.

- [ ] **8. `with_locale` never restores the ContextVar.** It calls
  `set_locale(lang)` and drops the token it returns (`bot/decorators.py`);
  `bot/commands.py` resets its own properly. Each python-telegram-bot update runs
  in its own context, so nothing has leaked yet — it is a trap for the first
  handler that shares one.

- [ ] **9. `query: Any, db: Any` through every callback.** mypy checks nothing
  inside roughly fifty handlers. Two small Protocols would restore it without
  changing the structure.
