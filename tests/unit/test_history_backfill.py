"""`backfill_history` — best-effort, right after a product is added.

Every outcome that is not "wrote some points" must look the same to the
caller: no registry, no provider, every provider missing, or an error — all
`None`, so a broken or absent plugin never looks like a failed add. No network
here; providers are fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from price_tracker.bot.handlers._history_backfill import backfill_history
from price_tracker.core.history_base import AbstractHistoryProvider, HistoryPoint, HistoryResult
from price_tracker.core.registry import HistoryRegistry

if TYPE_CHECKING:
    import httpx

ADDED_AT = datetime(2026, 6, 1, tzinfo=UTC)


def _point(day: int, price: str, *, month: int = 1, year: int = 2026) -> HistoryPoint:
    return HistoryPoint(observed_at=datetime(year, month, day, tzinfo=UTC), price=Decimal(price))


class _Provider(AbstractHistoryProvider):
    """A fake with a fixed result, an artificial delay, or a raise on demand.

    `name`/`priority` are typed `ClassVar` on the real base — ignored here
    since every test in this file wants a fresh, differently-named fake
    rather than a new subclass per name.
    """

    def __init__(
        self,
        name: str,
        priority: int,
        result: HistoryResult | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self.name = name  # type: ignore[misc]
        self.priority = priority  # type: ignore[misc]
        self._result = result if result is not None else HistoryResult()
        self._raises = raises
        self.calls = 0

    def can_handle(self, url: str) -> bool:
        return True

    async def fetch(self, url: str, client: httpx.AsyncClient) -> HistoryResult:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def _db(*, lowest: Decimal | None = None) -> Any:
    db = AsyncMock()
    db.add_price_history_bulk = AsyncMock(return_value=2)
    return db


async def test_no_registry_is_a_clean_miss() -> None:
    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=None,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None


async def test_no_provider_matches_is_a_clean_miss() -> None:
    registry = HistoryRegistry()
    provider = _Provider("keepa", 100)
    provider.can_handle = lambda url: False  # type: ignore[method-assign]
    registry.register(provider)

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://pccomponentes.com/x",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None
    assert provider.calls == 0


async def test_an_empty_result_is_a_clean_miss() -> None:
    registry = HistoryRegistry()
    registry.register(_Provider("keepa", 100, HistoryResult()))

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None


async def test_an_error_result_is_a_clean_miss_not_an_exception() -> None:
    registry = HistoryRegistry()
    registry.register(_Provider("keepa", 100, HistoryResult(error="site changed shape")))

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None


async def test_a_provider_that_raises_is_skipped_not_fatal() -> None:
    """`fetch` promises not to raise; a plugin edited outside review might anyway."""
    registry = HistoryRegistry()
    registry.register(_Provider("broken", 100, raises=True))

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None


async def test_a_miss_falls_through_to_the_next_provider_by_priority() -> None:
    registry = HistoryRegistry()
    low_priority_hit = _Provider(
        "precioreal", 50, HistoryResult(points=(_point(1, "40"),), currency="EUR")
    )
    high_priority_miss = _Provider("keepa", 100, HistoryResult())
    registry.register(low_priority_hit)
    registry.register(high_priority_miss)
    db = _db()

    outcome = await backfill_history(
        db=db,
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is not None
    assert outcome.source == "precioreal"
    assert high_priority_miss.calls == 1  # asked first, priority 100 over 50
    assert low_priority_hit.calls == 1  # then this one, which had the points


async def test_a_hit_stops_the_walk_the_lower_priority_provider_is_never_asked() -> None:
    registry = HistoryRegistry()
    winner = _Provider("keepa", 100, HistoryResult(points=(_point(1, "40"),), currency="EUR"))
    loser = _Provider("precioreal", 50, HistoryResult(points=(_point(1, "35"),), currency="EUR"))
    registry.register(winner)
    registry.register(loser)

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is not None
    assert outcome.source == "keepa"
    assert loser.calls == 0


async def test_a_point_at_or_after_the_add_moment_is_dropped() -> None:
    """A provider's "today" entry must not double up with the bot's own first read."""
    registry = HistoryRegistry()
    points = (
        _point(1, "40"),  # before ADDED_AT
        HistoryPoint(observed_at=ADDED_AT, price=Decimal("39")),  # exactly at
        HistoryPoint(observed_at=datetime(2026, 7, 1, tzinfo=UTC), price=Decimal("38")),  # after
    )
    registry.register(_Provider("keepa", 100, HistoryResult(points=points, currency="EUR")))
    db = _db()

    outcome = await backfill_history(
        db=db,
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is not None
    written_points = db.add_price_history_bulk.await_args.args[1]
    assert len(written_points) == 1
    assert written_points[0].price == Decimal("40")


async def test_a_non_positive_price_is_dropped() -> None:
    """A provider's way of marking "unavailable that day", not a price."""
    registry = HistoryRegistry()
    points = (_point(1, "40"), _point(2, "0"), _point(3, "-5"))
    registry.register(_Provider("keepa", 100, HistoryResult(points=points, currency="EUR")))
    db = _db()

    outcome = await backfill_history(
        db=db,
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is not None
    assert len(db.add_price_history_bulk.await_args.args[1]) == 1


async def test_all_points_dropped_leaves_no_outcome() -> None:
    registry = HistoryRegistry()
    points = (HistoryPoint(observed_at=ADDED_AT, price=Decimal("40")),)  # at cutoff: dropped
    registry.register(_Provider("keepa", 100, HistoryResult(points=points, currency="EUR")))

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None


async def test_matching_currency_needs_no_conversion() -> None:
    registry = HistoryRegistry()
    registry.register(
        _Provider("keepa", 100, HistoryResult(points=(_point(1, "40"),), currency="EUR"))
    )
    db = _db()

    await backfill_history(
        db=db,
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    written_points = db.add_price_history_bulk.await_args.args[1]
    assert written_points[0].price == Decimal("40")


async def test_a_currency_this_deployment_cannot_convert_to_is_a_clean_miss() -> None:
    """EUR is the only conversion target this deployment has."""
    registry = HistoryRegistry()
    registry.register(
        _Provider("keepa", 100, HistoryResult(points=(_point(1, "40"),), currency="USD"))
    )

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="GBP",
        added_at=ADDED_AT,
    )

    assert outcome is None


async def test_the_outcome_carries_the_count_source_and_earliest_date() -> None:
    registry = HistoryRegistry()
    points = (_point(15, "45"), _point(1, "40"), _point(10, "42"))
    registry.register(_Provider("keepa", 100, HistoryResult(points=points, currency="EUR")))

    outcome = await backfill_history(
        db=_db(),
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is not None
    assert outcome.count == 2  # add_price_history_bulk mock returns 2
    assert outcome.source == "keepa"
    assert outcome.first_observed_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert outcome.lowest_price == Decimal("40")


async def test_a_zero_write_count_is_treated_as_a_miss() -> None:
    """A defensive read: `add_price_history_bulk` returning 0 must not report success."""
    registry = HistoryRegistry()
    registry.register(
        _Provider("keepa", 100, HistoryResult(points=(_point(1, "40"),), currency="EUR"))
    )
    db = _db()
    db.add_price_history_bulk = AsyncMock(return_value=0)

    outcome = await backfill_history(
        db=db,
        client=None,  # type: ignore[arg-type]
        history_registry=registry,
        product_id=1,
        url="https://amazon.es/dp/B01",
        currency="EUR",
        added_at=ADDED_AT,
    )

    assert outcome is None
