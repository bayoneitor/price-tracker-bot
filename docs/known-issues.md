# Known issues

Defects and risks found in a read of the code on 2026-09-07, with the evidence
that established each one, and what a fix changed once it landed.

Entries are not tagged with a commit hash: the commit that fixes an entry also
ticks it, so the hash would have to be written before it exists. The link runs
the other way — each of those commits names this file, so
`git log --grep known-issues` finds them.

New features are not here — those live in [roadmap.md](roadmap.md).

## Behaviour that lies

- [x] **1. The per-product check interval was never honoured.** **Fixed.** `_is_due` (`core/scheduler.py`) skips a product until its own interval has
  passed. The interval is a *minimum gap* — it cannot make a product checked more
  often than the sweep itself runs — so `/refresh` now says so when the number
  asked for is smaller than the sweep, rather than letting it imply otherwise.

  <details><summary>What it was</summary>

  `/refresh 3 30` wrote `products.check_interval_minutes`
  (`db/repository.py:334`), the listing card rendered it
  (`bot/handlers/product_list.py:122`), and the scheduler read it nowhere:
  `run_check_all` walked every active product on every tick and
  `check_interval_minutes` appeared nowhere in `core/`. The bot reported a setting
  it did not apply. It was on the README roadmap as "per-product check intervals
  honoured by the scheduler".
  </details>

- [x] **2. `/debug` fetched any URL with no SSRF check.** **Fixed.** `cmd_debug` applies `validate_public_url` before anything is fetched. It needs
  its own check rather than relying on the shared client, because it also reaches
  for curl_cffi and Scrapling, which never touch it.

- [x] **3. A redirect walked past the SSRF guard.** **Fixed.** The
  shared client is built with a request hook that re-applies the boundary to
  every hop, so a public host answering `302 → http://169.254.169.254/` is
  refused at the second request. Resolution runs in a thread — an event hook sits
  on the event loop, and a slow DNS answer there would stall every handler. The
  note on `validate_public_url` that called redirects "a separate, narrower
  vector … not covered here" now points at the hook instead.

## Operational risk

- [ ] **4. The whole bot package is excluded from coverage.**
  `pyproject.toml` omits `src/price_tracker/bot/*` — 7,539 lines, the largest
  module and the one under most change. The reported figure describes everything
  *except* the user-facing surface, so a regression there fails no gate.

- [x] **5. CSV import had no bound.** **Fixed.** A file over 1 MB is refused
  before it is downloaded — Telegram reports the size up front, so pulling one in
  order to reject it is waste — and the row loop stops at 500, reporting where it
  stopped. It stops rather than refusing: the rows already imported are good ones,
  and discarding them to punish a long file helps nobody.

  <details><summary>What it was</summary>

  `download_to_memory` took the whole file and the loop scraped every row in
  turn. Telegram caps a bot download at 20 MB, which is still tens of thousands of
  URLs and as many outbound requests.
  </details>

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
