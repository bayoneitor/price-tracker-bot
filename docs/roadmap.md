# Proposed features

Candidate work, not committed work. Each entry states the problem it solves, what
already exists to build on, and roughly what it would touch — so the cost is
visible next to the value and nothing here has to be re-derived from scratch.

Written after a read of the code on 2026-09-07. Ordered by value against effort,
not by preference.

Fixes to existing behaviour are **not** here — this file is only about things the
bot cannot do yet. The one exception worth naming, because it looks like a
feature and is really a repair: **the per-product check interval is stored,
displayed and never honoured by the scheduler** (`/refresh` writes
`products.check_interval_minutes`, `run_check_all` never reads it). That is
already on the README roadmap as "per-product check intervals honoured by the
scheduler".

## What already exists

So nobody proposes it twice:

| | |
|---|---|
| Alert triggers | `any_drop`, `percentage`, `absolute`, `target` (`core/alert.py`) |
| Back-in-stock alerts | yes, and routed through the same mute/quiet-hours gate (`core/scheduler.py`) |
| Operational notices | listing gone, per-domain quarantine, grouped per store with buttons |
| Delivery settings | mute, quiet hours, timezone, rate limit, digest — per user and per product |
| Groups | membership, side-by-side table, comparison chart, "who has been cheapest" |
| Charts | one product over 90 days; a group compared, up to eight series |
| Data | CSV import/export, per-user |
| Observability | Prometheus exporter, 14-panel Grafana dashboard |

---

## 1. All-time-low alerts

**The problem.** Every trigger available today measures a drop against the price
the product had *when you started tracking it*, or against a number you typed.
None of them answers the question that actually decides a purchase: **is this the
cheapest it has ever been?** A product that fell 5% from a price that was already
a peak reads as good news; one sitting at its historic floor after a slow slide
says nothing at all.

**What it builds on.** `products.lowest_price` is already maintained on every
successful check (`repository.update_price`), so the datum exists — nothing needs
recording first, and it applies retroactively to everything already tracked.

**Shape.** A fifth `ThresholdType` in `core/alert.py:29`, evaluated against
`lowest_price` instead of `initial_price`; one more button in the threshold
keyboard (`bot/keyboards.py`) and in the edit panel. Migration only if the choice
needs its own column — `threshold_type` already carries the discriminator.

**Effort.** Small. This is the highest ratio on the list.

---

## 2. "Is this a good price?" — where today sits in the product's own history

**The problem.** The chart shows the shape of the history and leaves the reading
to the eye. The question behind it is arithmetic: *how does today compare to every
other day?*

**Shape.** One line on the product card: *"€429 — cheaper than 85% of the last 90
days; its floor is €399."* A percentile over the change points already fetched for
the chart, so no new query. Sits beside `📉 Min` in `_product_card`
(`bot/handlers/product_list.py`).

**Care needed.** It must stay descriptive. "Cheaper than 85% of the last 90 days"
is a fact; "buy now" is a forecast this data cannot support, and a price tracker
that guesses loses the trust that makes it useful.

**Effort.** Small — a pure function over data already in hand, plus a line in the
card.

---

## 3. A target for a whole group

**The problem.** Groups answer "which of these is cheapest"; they cannot yet
answer *"tell me when any of them drops below €400"*. Today that means setting the
same target on each member and getting an alert per product, when the thing you
care about is the group.

**What it builds on.** `product_groups` and `product_group_members` (migration
016), and the per-product target that already exists.

**Shape.** A nullable `target_price` on `product_groups`, evaluated where product
alerts are; one alert naming the member that crossed, not one per member. A
button in the group view (`bot/handlers/groups_view.py`).

**Effort.** Medium — a migration, an evaluation hook, and a decision about how a
group target interacts with a member's own.

---

## 4. Dedicated scrapers for Spanish retailers

**The problem.** Of the 17 built-in scrapers (`docs/scrapers.md`), the ones aimed
at Europe cover MediaMarkt, Otto, Zalando and Apple/Google's stores. The shops a
Spanish buyer compares against each other — PcComponentes, El Corte Inglés,
Carrefour, Fnac, Worten, Coolmod — all fall to `GenericScraper`, which works only
where the site publishes clean JSON-LD, microdata, OpenGraph or RDFa. When it
does not, the product simply cannot be tracked.

Comparing one product across shops is the case groups were built for, so the
value of the whole feature is capped by how many of those shops can be read.

**What it builds on.** `plugins/` is a documented extension point
(`docs/plugins.md`) and the scraper base class handles the fetch, the currency
and the block detection. A new scraper is a `can_handle` plus an extraction
strategy.

**Effort.** Small per shop, and each one is independent — this can be done one
retailer at a time, and each is worth having on its own.

---

## 5. A periodic summary

**The problem.** The bot only speaks when a threshold fires. Nothing tells you
*"here is what happened this week"*, which is the report you would actually read.

**What it builds on.** The whole digest pipeline — `DigestService`, the
`digest_queue` table, quiet hours and the per-user timezone — plus the chart
renderer. Very little of this is new machinery.

**Shape.** A scheduled job per user, at an hour of their choosing, rendering what
moved, what is near its target, what has been failing to read, and the group
standings. One message with one chart.

**Effort.** Medium, almost all of it assembly.

---

## 6. Price-rise alerts

Symmetric to the drop triggers, for a product you watch because you do not want
it to get more expensive — or to learn that a deal has ended. `highest_price` is
already maintained. Small, and largely the same code path as item 1.

## 7. Filtering the listing

With enough products the listing is a long scroll. Filters worth having: by shop,
"dropped this week", "near its target", "failing to read". The listing is already
paginated and re-rendered from a pure function (`build_list_view`), so a filter is
a parameter and a row of buttons rather than a new screen.

## 8. Group budgets

*"These five come to €1,240; your budget is €1,000; you need €240 more of
drops."* A shopping list that watches itself. Another view over groups, next to
the comparison — a nullable `budget` column and a rendering function.

## 9. The history as text

`12/08 €449 → €429` — copyable, screen-reader friendly, and readable where an
image is not. The change-point query added for the chart returns exactly these
rows already, so this is a renderer and a button.

## 10. Reference lines on the comparison chart

A single product's chart draws its target line; the group comparison marks
nothing. Each series' all-time low, and any group target from item 3, would make
the chart answer "how far from the floor" without a second screen.

## 11. Sharing a group

Export a group and import it from a forwarded message, to hand someone "my
monitor shortlist". CSV import/export already exists per user; this is a scoped
variant plus a way in from a forwarded document.

## 12. Outbound webhooks

Let a price change trigger something else — Home Assistant, n8n, a spreadsheet.
`NotifierFn` in `core/scheduler.py` is already the extension point with exactly
this shape: the scheduler hands it the alert and a structured payload and asks
only whether delivery happened.

Niche, but it is the request a self-hosted tracker eventually gets, and the seam
is already there.

---

## Deliberately not proposed

- **Buy/sell recommendations or price forecasting.** The data supports describing
  the past, not predicting the future. Item 2 stays descriptive on purpose.
- **Scraping behind logins or paywalls.** Out of scope for a tool that identifies
  itself and honours per-domain quarantine.
- **Shortening the check interval to catch flash sales.** The polite pacing and
  the quarantine tiers exist because sites block; competing with that would trade
  a working tracker for a faster one that gets banned.
