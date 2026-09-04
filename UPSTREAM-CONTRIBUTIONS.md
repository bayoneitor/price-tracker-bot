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

### CI on #29 and #30

No checks have run yet. That is expected, not a failure: GitHub requires a
maintainer to approve the first workflow run on a pull request coming from a
fork of someone who has not contributed before. A **"Approve and run
workflows"** button appears on the PR page until then.

The same workflow also runs on `push` to any branch, so it does execute in this
fork: https://github.com/bayoneitor/price-tracker-bot/actions

### Why the command menu is not submitted

`feat/telegram-command-menu` is branched off `fix/i18n-english-msgids-and-spanish`,
not off `main`. It needs that branch: the menu publishes one command list per
locale via `language_code`, and the Spanish catalog only exists there.

Opening it now would produce a pull request carrying both commits (62 files),
which makes review harder for no benefit. Open it once #30 is merged, against
an updated `main`, and the diff drops to the 12 files that are actually about
the menu.

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

## Testing all three together

This `integration` branch merges all three. It exists so the whole set can be
run before upstream has merged anything; it is never pushed to a pull request.

```bash
git switch integration
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 923 tests
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
