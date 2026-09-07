# Drop-in plugins

This directory is a runtime extension point for two kinds of plugin, both
auto-discovered at startup by `core.registry.discover_dropin_plugins`:

- a **scraper** — subclasses `AbstractScraper` from
  `price_tracker.core.scraper_base`, reads a product's *current* price. See
  [docs/plugins.md](../docs/plugins.md).
- a **history provider** — subclasses `AbstractHistoryProvider` from
  `price_tracker.core.history_base`, backfills a product's *past* prices at
  add time. See [docs/history-providers.md](../docs/history-providers.md).

One `.py` file may define either kind, or both. A file whose name starts with
`_` is not auto-loaded — use that for a shared helper another plugin file
imports.

Every file in this directory except this README is gitignored, so plugins
live here without being committed to the public repository. Restart the bot
to pick up a new or changed file.
