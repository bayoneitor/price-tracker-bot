"""Store name shown alongside tracked products.

A tracked URL is often a wall of tracking parameters, and an alert arrives with
no context — the shop it points at is the useful half.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_tracker.core.alert import PriceAlert, format_alert, format_back_in_stock
from price_tracker.core.url_utils import store_label


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"domain": "mediamarkt.es"}, "mediamarkt.es"),
        ({"url": "https://www.mediamarkt.es/es/product/_x-1.html"}, "mediamarkt.es"),
        # Subdomains collapse to the registrable domain: the shop, not the host.
        ({"url": "https://es-store.msi.com/products/y"}, "msi.com"),
        ({"url": "https://www.amazon.co.uk/dp/B01"}, "amazon.co.uk"),
        # The stored column wins; deriving is only the fallback.
        ({"domain": "amazon.es", "url": "https://example.com/p"}, "amazon.es"),
        ({"domain": "www.amazon.es"}, "amazon.es"),
        # Nothing usable → empty, so callers omit the line instead of printing junk.
        ({"url": "not a url"}, ""),
        ({}, ""),
        ({"domain": "   "}, ""),
    ],
)
def test_store_label(kwargs: dict[str, str], expected: str) -> None:
    assert store_label(**kwargs) == expected


def _alert(url: str) -> PriceAlert:
    return PriceAlert(
        product_id=3,
        product_name="Monitor",
        url=url,
        old_price=Decimal("349"),
        new_price=Decimal("259"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )


def test_alert_links_the_tracked_url_labelled_with_the_store() -> None:
    """The href stays the submitted URL, HTML-escaped, tracking params and all."""
    url = "https://www.mediamarkt.es/es/product/_x-1667552.html?gclid=abc&utm_source=g"
    text = format_alert(_alert(url))
    # `&` is escaped for the attribute; the client unescapes it when opening.
    assert (
        'href="https://www.mediamarkt.es/es/product/_x-1667552.html?gclid=abc&amp;utm_source=g"'
        in text
    )
    assert ">🌐 mediamarkt.es</a>" in text
    assert "View product" not in text


def test_alert_falls_back_to_a_generic_label_without_a_domain() -> None:
    """A URL with no public suffix still produces a usable link."""
    text = format_alert(_alert("https://localhost:8080/p/1"))
    assert "View product" in text
    assert 'href="https://localhost:8080/p/1"' in text


def test_alert_escapes_a_hostile_url() -> None:
    """The URL is user-supplied and lands in an href attribute."""
    text = format_alert(_alert('https://evil.example/"><script>x</script>'))
    assert "<script>" not in text
    assert "&quot;" in text or "&lt;" in text


def test_back_in_stock_also_labels_the_store() -> None:
    text = format_back_in_stock(
        product_name="Monitor",
        url="https://www.mediamarkt.es/es/product/_x-1.html",
        price=Decimal("259"),
        currency="EUR",
    )
    assert ">🌐 mediamarkt.es</a>" in text


def test_alert_is_translated() -> None:
    """Regression: these were plain English literals, never wrapped in _()."""
    from price_tracker.bot.messages import get_translation, reset_locale, set_locale

    get_translation.cache_clear()
    alert = _alert("https://www.mediamarkt.es/es/product/_x-1.html")
    token = set_locale("es")
    try:
        assert "¡Bajada de precio!" in format_alert(alert)
    finally:
        reset_locale(token)
        get_translation.cache_clear()
