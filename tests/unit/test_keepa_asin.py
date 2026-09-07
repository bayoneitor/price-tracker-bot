"""`extract_amazon_asin` / `keepa_domain_code` — shared by the public Keepa-graph
button and, per the plan, the private Keepa history plugin, which needs the
same domain id to open the right storefront's product page.
"""

from __future__ import annotations

import pytest

from price_tracker.core.url_utils import extract_amazon_asin, keepa_domain_code


@pytest.mark.parametrize(
    ("url", "asin"),
    [
        ("https://www.amazon.es/dp/B0CX23V2ZK/ref=xyz", "B0CX23V2ZK"),
        ("https://www.amazon.com/Some-Title/dp/B08N5WRWNW", "B08N5WRWNW"),
        ("https://www.amazon.co.uk/gp/product/B01N5IB20Q", "B01N5IB20Q"),
        ("https://www.amazon.de/product/B123456789", "B123456789"),
        ("https://www.amazon.es/dp/B0CX23V2ZK?th=1", "B0CX23V2ZK"),
    ],
)
def test_the_asin_is_pulled_from_a_real_product_link(url: str, asin: str) -> None:
    assert extract_amazon_asin(url) == asin


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.es/s?k=monitor",
        "https://www.amazon.es/gp/cart/view.html",
        "https://www.pccomponentes.com/monitor",
        "",
    ],
)
def test_a_link_with_no_asin_yields_none(url: str) -> None:
    assert extract_amazon_asin(url) is None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://www.amazon.es/dp/B01", 9),
        ("https://amazon.es/dp/B01", 9),  # no www — must not require it
        ("https://www.amazon.com/dp/B01", 1),
        ("https://www.amazon.co.uk/dp/B01", 2),
        ("https://www.amazon.it/dp/B01", 8),
    ],
)
def test_the_domain_code_matches_the_storefront(url: str, code: int) -> None:
    assert keepa_domain_code(url) == code


def test_an_unmapped_tld_yields_none_rather_than_a_wrong_code() -> None:
    assert keepa_domain_code("https://www.amazon.se/dp/B01") is None


def test_a_non_amazon_url_still_returns_a_code_by_tld_alone() -> None:
    """`keepa_domain_code` only reads the TLD — callers must also check
    `extract_amazon_asin`, which is what actually gates "is this Amazon"."""
    assert keepa_domain_code("https://www.pccomponentes.com/monitor") == 1
