-- Where a price reading came from: the bot's own check, or a backfill provider.
--
-- Without this, every row in `price_history` looked the same regardless of how
-- it got there. Once a product can start with months of imported prices
-- (docs/plans/2026-09-07-history-backfill-private-plugins.md), the chart needs
-- to draw them differently from a live read, and a reader needs to see where a
-- product's own memory of itself ends and an import begins.
--
-- `price_history.source`: NULL means the bot's own check, same as every row
-- written before this migration — no backfill, no meaning change for existing
-- data. Set to a provider's name (e.g. 'keepa') on an imported row.
--
-- `products.history_source` / `history_backfilled_at`: which provider last
-- backfilled this product and when, so the product's own screen and the chart
-- caption can say so without joining price_history to find out.
ALTER TABLE price_history ADD COLUMN source TEXT;
ALTER TABLE products ADD COLUMN history_source TEXT;
ALTER TABLE products ADD COLUMN history_backfilled_at TEXT;
