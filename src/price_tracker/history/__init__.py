"""No built-in history providers.

Unlike `price_tracker.scrapers`, which `discover_builtin_scrapers` scans on
every startup, this package ships empty — it exists so a provider written for
this seam has a real home to import against (`price_tracker.history_base`
would work just as well; this is where the operator docs point) and so tests
have a package to register fakes under without touching `plugins/`.

Providers come from `plugins/`, discovered the same way drop-in scrapers are
— see `core.registry.discover_dropin_plugins` and
`docs/history-providers.md`.
"""

from __future__ import annotations
