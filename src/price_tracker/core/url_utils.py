"""URL parsing utilities."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import tldextract

_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public destination (SSRF guard)."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for loopback/private/link-local/reserved/multicast/unspecified addresses."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` if ``url`` is not a safe public http(s) target.

    SSRF guard for user-supplied product URLs. Blocks non-http(s) schemes and
    hosts that are — or resolve to — loopback/private/link-local/reserved
    addresses (e.g. ``http://localhost``, ``http://127.0.0.1``,
    ``http://169.254.169.254`` cloud-metadata, ``http://192.168.x.x``,
    ``http://[::1]``). An unresolvable host is allowed (it cannot be connected to,
    so it carries no SSRF risk); the scrape simply fails later with a normal error.

    This validates the user-supplied URL at the storage boundary. The hops of a
    redirect chain are covered separately, by the request hook the shared client
    is built with (`core/http_client.build_client`).
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise UnsafeURLError(f"host {host} is a non-public address")
        return

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # unresolvable → not reachable → not an SSRF risk
    for info in infos:
        addr = info[4][0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise UnsafeURLError(f"host {host} resolves to non-public address {addr}")


def extract_etld_plus_one(url: str) -> str:
    """Return the registrable domain (eTLD+1) of a URL.

    Uses the public suffix list to correctly handle multi-part TLDs (.co.uk).
    Returns empty string when the URL has no public suffix or is malformed.
    """
    if not url or not isinstance(url, str):
        return ""
    parts = _extractor(url)
    if not parts.suffix or not parts.domain:
        return ""
    return f"{parts.domain}.{parts.suffix}"


def store_label(*, url: str = "", domain: str = "") -> str:
    """Return a short, human-readable store name for a tracked product.

    Prefers the stored ``domain`` column and falls back to deriving it from the
    URL, so rows written before the column existed still render. The result is
    the registrable domain (``mediamarkt.es``) rather than the full host: it is
    what a reader recognises as "the shop", and it stays stable across a site's
    regional subdomains. Returns an empty string when neither input yields one,
    so callers can omit the line rather than print a placeholder.
    """
    candidate = (domain or "").strip()
    if not candidate:
        candidate = extract_etld_plus_one(url)
    return candidate.removeprefix("www.")


_ASIN_RE = re.compile(r"/(?:dp|gp/product|product)/([A-Z0-9]{10})(?:[/?]|$)")

# Keepa addresses each Amazon storefront by a small integer, not by TLD — its
# own public API and the free `graph.keepa.com` PNG endpoint both take this
# id. Shared here rather than duplicated between the public Keepa-graph button
# and the private Keepa history plugin (docs/history-providers.md), which
# needs the same id to open the right storefront's product page.
KEEPA_DOMAIN_CODES: dict[str, int] = {
    "com": 1,
    "co.uk": 2,
    "de": 3,
    "fr": 4,
    "co.jp": 5,
    "ca": 6,
    "cn": 7,
    "it": 8,
    "es": 9,
    "in": 10,
    "com.mx": 11,
    "com.br": 12,
}


def extract_amazon_asin(url: str) -> str | None:
    """Pull the ASIN out of an Amazon product URL, or None if there isn't one.

    Matches `/dp/`, `/gp/product/` and `/product/` — the three paths Amazon
    actually links a product page from — each followed by exactly ten
    alphanumerics. A URL carrying no ASIN in one of those shapes (a search
    results page, a cart link) yields None rather than a wrong guess.
    """
    if not url:
        return None
    match = _ASIN_RE.search(url)
    return match.group(1) if match else None


def keepa_domain_code(url: str) -> int | None:
    """Keepa's numeric domain id for an Amazon URL's storefront, or None if unmapped.

    Derived from the eTLD+1's suffix (`extract_etld_plus_one`), so
    `www.amazon.co.uk/...` and `amazon.co.uk/...` resolve the same way.
    """
    parts = _extractor(url)
    if not parts.suffix:
        return None
    return KEEPA_DOMAIN_CODES.get(parts.suffix)
