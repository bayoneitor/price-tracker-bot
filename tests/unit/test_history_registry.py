"""`HistoryRegistry` — the seam a backfill provider plugs into.

Unlike `ScraperRegistry.resolve`, which stops at the first scraper willing to
read the current price, more than one provider can plausibly hold history for
the same URL, so `resolve_all` returns every match in priority order rather
than the first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from price_tracker.core.history_base import AbstractHistoryProvider, HistoryPoint, HistoryResult
from price_tracker.core.registry import HistoryRegistry

if TYPE_CHECKING:
    import httpx


class _FakeKeepa(AbstractHistoryProvider):
    name = "keepa"
    priority = 100

    def can_handle(self, url: str) -> bool:
        return "amazon." in url

    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult:
        return HistoryResult(
            points=(
                HistoryPoint(observed_at=datetime(2026, 1, 1, tzinfo=UTC), price=Decimal("9")),
            ),
            currency="EUR",
        )


class _FakeGeneric(AbstractHistoryProvider):
    name = "generic-history"
    priority = 10

    def can_handle(self, url: str) -> bool:
        return True

    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult:
        return HistoryResult(error="not implemented")


def test_registered_providers_are_ordered_by_priority() -> None:
    r = HistoryRegistry()
    r.register(_FakeGeneric())
    r.register(_FakeKeepa())

    assert [p.name for p in r.list_providers()] == ["keepa", "generic-history"]


def test_resolve_all_returns_every_willing_provider_in_order() -> None:
    r = HistoryRegistry()
    r.register(_FakeGeneric())
    r.register(_FakeKeepa())

    matches = r.resolve_all("https://www.amazon.es/dp/B01")

    assert [p.name for p in matches] == ["keepa", "generic-history"]


def test_resolve_all_excludes_a_provider_that_declines() -> None:
    """Keepa only handles Amazon; a PcComponentes URL never reaches it."""
    r = HistoryRegistry()
    r.register(_FakeGeneric())
    r.register(_FakeKeepa())

    matches = r.resolve_all("https://www.pccomponentes.com/monitor")

    assert [p.name for p in matches] == ["generic-history"]


def test_resolve_all_on_no_match_is_an_empty_list_not_none() -> None:
    r = HistoryRegistry()
    r.register(_FakeKeepa())

    assert r.resolve_all("https://www.pccomponentes.com/monitor") == []


def test_register_rejects_a_duplicate_name() -> None:
    r = HistoryRegistry()
    r.register(_FakeKeepa())
    with pytest.raises(ValueError, match="already registered"):
        r.register(_FakeKeepa())


def test_len_and_iteration() -> None:
    r = HistoryRegistry()
    assert len(r) == 0
    r.register(_FakeKeepa())
    r.register(_FakeGeneric())
    assert len(r) == 2
    assert {p.name for p in r} == {"keepa", "generic-history"}


async def test_a_provider_that_fails_reports_it_rather_than_raising() -> None:
    """The contract `fetch` promises: no data is `error`, not an exception."""
    result = await _FakeGeneric().fetch("https://example.com", None)  # type: ignore[arg-type]

    assert result.points == ()
    assert result.error == "not implemented"


async def test_a_provider_with_data_carries_its_currency() -> None:
    result = await _FakeKeepa().fetch("https://amazon.es/dp/B01", None)  # type: ignore[arg-type]

    assert len(result.points) == 1
    assert result.currency == "EUR"
    assert result.error is None
