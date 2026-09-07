"""Plugin discovery and registry for scrapers and history providers."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from typing import TYPE_CHECKING

from price_tracker.core.history_base import AbstractHistoryProvider
from price_tracker.core.scraper_base import AbstractScraper

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


class ScraperRegistry:
    """Holds registered scraper instances and resolves URLs to the right one."""

    def __init__(self) -> None:
        self._scrapers: list[AbstractScraper] = []
        self._names: set[str] = set()

    def register(self, scraper: AbstractScraper) -> None:
        """Register a scraper instance. Raises if name already registered."""
        if scraper.name in self._names:
            raise ValueError(f"Scraper '{scraper.name}' already registered")
        self._scrapers.append(scraper)
        self._names.add(scraper.name)
        # Re-sort by priority (higher = first)
        self._scrapers.sort(key=lambda s: s.priority, reverse=True)

    def list(self) -> list[AbstractScraper]:
        """Return scrapers in priority order (highest first)."""
        return list(self._scrapers)

    def resolve(self, url: str) -> AbstractScraper | None:
        """Return the first scraper whose `can_handle(url)` is True. None if none match."""
        for s in self._scrapers:
            if s.can_handle(url):
                return s
        return None

    def __iter__(self) -> Iterator[AbstractScraper]:
        return iter(self._scrapers)

    def __len__(self) -> int:
        return len(self._scrapers)


def discover_builtin_scrapers(registry: ScraperRegistry) -> None:
    """Scan `price_tracker.scrapers` package and register every Scraper subclass found."""
    import price_tracker.scrapers as scrapers_pkg

    for module_info in pkgutil.iter_modules(scrapers_pkg.__path__):
        module_name = f"price_tracker.scrapers.{module_info.name}"
        module = importlib.import_module(module_name)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, AbstractScraper)
                and attr is not AbstractScraper
            ):
                try:
                    registry.register(attr())
                    logger.info("Registered built-in scraper: %s", attr.name)
                except ValueError:
                    # Already registered (subclass present in multiple modules)
                    pass


class HistoryRegistry:
    """Holds registered history-provider instances and resolves a URL to all of them.

    Unlike `ScraperRegistry.resolve`, which stops at the first match because
    exactly one scraper reads the current price, more than one provider can
    plausibly hold history for the same URL — `resolve_all` returns every
    match in priority order and the caller (`_add_product`'s backfill step)
    keeps walking until one returns points or all have been asked.
    """

    def __init__(self) -> None:
        self._providers: list[AbstractHistoryProvider] = []
        self._names: set[str] = set()

    def register(self, provider: AbstractHistoryProvider) -> None:
        """Register a provider instance. Raises if the name is already taken."""
        if provider.name in self._names:
            raise ValueError(f"History provider '{provider.name}' already registered")
        self._providers.append(provider)
        self._names.add(provider.name)
        self._providers.sort(key=lambda p: p.priority, reverse=True)

    def list_providers(self) -> list[AbstractHistoryProvider]:
        """Return providers in priority order (highest first).

        Not named `list`, unlike `ScraperRegistry`'s twin: mypy (strict,
        3.12) misresolves `list[AbstractHistoryProvider]` in *this* class's
        own return annotation to the method being defined rather than the
        builtin once a second class in the module defines a same-named
        method — reproduced in isolation, not a design choice.
        """
        return list(self._providers)

    def resolve_all(self, url: str) -> list[AbstractHistoryProvider]:
        """Every provider willing to try `url`, in priority order."""
        return [p for p in self._providers if p.can_handle(url)]

    def __iter__(self) -> Iterator[AbstractHistoryProvider]:
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)


def discover_dropin_plugins(
    scraper_registry: ScraperRegistry,
    history_registry: HistoryRegistry,
    plugin_dir: Path,
) -> None:
    """Load any *.py file in `plugin_dir` once, registering both kinds it may define.

    One pass rather than one per plugin type: a plugin file is executed to be
    scanned, and executing it twice would run whatever top-level code it
    carries twice too. `plugins/precioreal.py` defines only a history
    provider today; a future drop-in scraper works the same way it always
    has, registered in the same pass.

    An import error in one file is logged and skipped rather than raised — the
    plugin directory is operator content, edited on the running host outside
    a review, and a typo in one file must not stop the bot from starting.
    """
    if not plugin_dir.is_dir():
        return
    for file in plugin_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"plugins.{file.stem}", file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to load plugin %s — skipped", file)
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if not isinstance(attr, type):
                continue
            if issubclass(attr, AbstractScraper) and attr is not AbstractScraper:
                try:
                    scraper_registry.register(attr())
                    logger.info("Registered drop-in scraper: %s (from %s)", attr.name, file)
                except ValueError:
                    pass
            elif issubclass(attr, AbstractHistoryProvider) and attr is not AbstractHistoryProvider:
                try:
                    history_registry.register(attr())
                    logger.info(
                        "Registered drop-in history provider: %s (from %s)", attr.name, file
                    )
                except ValueError:
                    pass
