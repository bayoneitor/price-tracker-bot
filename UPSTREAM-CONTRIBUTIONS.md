# Upstream contributions — status

Working notes for the changes sent to
[bernalli/price-tracker-bot](https://github.com/bernalli/price-tracker-bot)
from this fork. **This file lives only on the `integration` branch** and is
deliberately not part of any pull request.

Last updated: 2026-09-04.

## Pull requests

| PR | Branch | Status | Size |
| --- | --- | --- | --- |
| [#29](https://github.com/bernalli/price-tracker-bot/pull/29) — `fix(scrapers): restore MediaMarkt price extraction` | `fix/mediamarkt-price-extraction` | Open, awaiting review | 7 files, +408/−28 |
| [#30](https://github.com/bernalli/price-tracker-bot/pull/30) — `fix(i18n): make every user-facing string translatable, add Spanish` | `fix/i18n-english-msgids-and-spanish` | Open, awaiting review | 50 files, +6562/−1066 |
| — Telegram command menu | `feat/telegram-command-menu` | **Not submitted yet**, waiting on #30 | 12 files, +1131/−412 |
| — Store name on product views | `feat/store-name-in-product-views` | **Not submitted yet**, waiting on #30 | 11 files, +560/−80 |
| — Paginated `/list` | `feat/paginated-product-list` | **Not submitted yet**, waiting on #30 and the store branch | 10 files, +700/−140 |
| — Closable edit panel | `fix/closable-edit-panel` | **Not submitted yet**, stacked on the `/list` branch | 6 files, +90/−20 |

### CI on #29 and #30

No checks have run yet. That is expected, not a failure: GitHub requires a
maintainer to approve the first workflow run on a pull request coming from a
fork of someone who has not contributed before. A **"Approve and run
workflows"** button appears on the PR page until then.

The same workflow also runs on `push` to any branch, so it does execute in this
fork: https://github.com/bayoneitor/price-tracker-bot/actions

### Why the last two are not submitted

Both are branched off `fix/i18n-english-msgids-and-spanish` rather than `main`,
and both need it: the menu publishes one command list per locale via
`language_code`, and the store change translates the alerts — neither works
without the catalogs that branch adds.

Opening either now would produce a pull request carrying #30's commit as well,
which makes review harder for no benefit. Open them once #30 is merged, against
an updated `main`; the diffs then drop to the files that are actually theirs.
The first two are siblings, so they can go in either order. `feat/paginated-product-list`
is stacked on `feat/store-name-in-product-views` — the product card shows the
store — so that one goes first.

Their catalog changes conflict with each other by construction — both add
msgids to the same `.po` files. Do not hand-merge those: take one side, re-run
`./scripts/i18n.sh extract && ./scripts/i18n.sh update`, then translate whatever
comes back empty. That is how the conflict was resolved on this branch.

## What each change does

### #29 — MediaMarkt

MediaMarkt products stopped tracking entirely: the scraper resolved the domain
and read the product name, then reported "Price not found in page". Three
independent defects had to line up.

1. `_is_financing_offer` treated any `UnitPriceSpecification` as monthly
   financing. MediaMarkt states its strikethrough and loyalty-tier prices that
   way, so the real offer was discarded. **This one is not MediaMarkt-specific**
   — it affects every scraper that reads a JSON-LD offer, and it is the change
   worth watching after deploy.
2. The `Product` moved inside a schema.org `BuyAction`, so scanning only
   top-level `@type` never reached it.
3. The DOM fallback selected a wrapper element the current pages no longer
   render, which silently disabled it.

Both the old and the new page shapes are supported, with tests for each.

### #30 — i18n

Setting `LOCALE=en` still produced a largely Italian UI, worst of all in the
admin menus. `LOCALE` was never the cause: 206 Italian literals never reached
gettext at all, and 17 catalog msgids were themselves Italian. 223 strings
across 23 files now use English source strings; Spanish is added as a third
locale.

Four latent defects surfaced while doing it: untranslatable digest plurals,
operational-notice copy that was never marked for extraction, a `_()` call
hidden inside an f-string, and `pybabel compile` rejecting valid translations
over a literal percent in prose.

`LOCALE` semantics are unchanged and now documented: it is a **fallback**, not
an override. A client whose language has a catalog still wins over it — an
Italian Telegram gets Italian even under `LOCALE=en`.

### Command menu

Publishes the command list on startup via `setMyCommands`, so Telegram's Menu
button (bottom-left of the chat) shows tappable commands with descriptions.
English names only (the Italian aliases still work, they are just not listed),
one list per locale, and admin commands scoped to admins' own chats.

Adds `/import` as an English alias for `/importa`, which had none.

### Store name on product views

The shop a product comes from was only shown once, when the product was added.
It now appears on the price-drop alert, `/list`, the check button and the
history chart. In the alert it replaces the generic "View product" link text,
so the link reads as the store and stays clickable; the href is still the exact
URL submitted.

Also fixes a gap the i18n sweep missed: `format_alert`, `format_back_in_stock`
and `format_error_notification` were plain English literals that never went
through gettext, so the bot's most visible message stayed English for every
reader. That sweep looked for Italian text and these were already English.

### Paginated /list

`/list` sent one message per product plus a trailer: a dozen products meant
thirteen messages that buried the chat and could not be dismissed. It is now a
single message, edited in place — an index of every product, the selected one's
full card, ◀ ▶ paging, numbered jump buttons, the per-product actions, and a
close button that deletes the message. Typing a bare index number also jumps the
listing.

The selected index travels in the callback data rather than server state, so a
listing survives a restart. Every navigation re-reads the products and clamps
the index, so a stale button from a listing whose products were since deleted
lands somewhere valid.

### Closable edit panel

Tapping ✏️ Edit opens a *new* message instead of replacing the one it came
from, and it had no way out — no close button, nothing to edit it away. Closing
is now a shared mechanism (`CLOSE_CALLBACK` in `bot.keyboards`) that deletes
whichever message carries the button, used by both the listing and the edit
panel.

## Testing all three together

This `integration` branch merges all three. It exists so the whole set can be
run before upstream has merged anything; it is never pushed to a pull request.

```bash
git switch integration
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 958 tests
.venv/bin/ruff check . && .venv/bin/mypy --strict src/price_tracker tests
./scripts/audit_english.sh
```

To run the bot from this branch, rebuild the image — the running container
still has the pre-change code:

```bash
docker compose build && docker compose up -d
```

Things worth checking by hand once it is running:

- The Menu button lists the commands, in the language your Telegram client is
  set to, and admin commands appear only for admin accounts.
- A MediaMarkt product tracks and reports a price
  (verified against SKU 1667552 at 259 EUR).
- `/list` and the price-drop alert name the store the product comes from, and the
  alert's link is labelled with it instead of "View product".
- `/list` is one message: paging with the arrows and the numbered buttons, jumping by
  typing an index number, and closing it removes the message from the chat.
- The ✏️ Edit panel closes too, and doing so does not stop a typed number from steering
  a listing still open above it.
- With `LOCALE=en` and a Spanish or English client, no Italian text appears
  anywhere — the admin menus were the worst offenders.

## Keeping the branches current

If upstream moves before the PRs are merged:

```bash
git fetch origin
git rebase origin/main fix/mediamarkt-price-extraction
git rebase origin/main fix/i18n-english-msgids-and-spanish
git rebase fix/i18n-english-msgids-and-spanish feat/telegram-command-menu
git push --force-with-lease fork fix/mediamarkt-price-extraction fix/i18n-english-msgids-and-spanish
```

`CHANGELOG.md` is the one file all three touch, so it is where a conflict will
show up. Merging #29 and #30 in either order will make the second one conflict
there; the fix is to keep both entries, not to pick one.
