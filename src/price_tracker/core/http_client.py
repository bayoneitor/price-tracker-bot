"""Shared httpx async client factory."""

from __future__ import annotations

import asyncio
import logging

import httpx

from price_tracker.core.url_utils import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)


async def _refuse_private_targets(request: httpx.Request) -> None:
    """Re-apply the SSRF boundary to every request, redirects included.

    `validate_public_url` guards the URL a user hands over, at the point it is
    stored. It cannot guard where that URL leads: the client follows redirects,
    so a public host answering `302 → http://169.254.169.254/` was fetched
    anyway. The hook runs once per request in a chain, which is the only place
    that sees the hops.

    Resolution blocks, so it runs in a thread — an event hook is on the event
    loop, and a slow DNS answer here would stall every other handler.
    """
    try:
        await asyncio.to_thread(validate_public_url, str(request.url))
    except UnsafeURLError:
        logger.warning("Refused request to a private target: %s", request.url.host)
        raise


def build_client(
    *,
    timeout: float = 30.0,
    connect_timeout: float = 10.0,
    max_connections: int = 10,
    max_keepalive_connections: int = 5,
) -> httpx.AsyncClient:
    """Build a configured httpx.AsyncClient.

    Caller is responsible for `await client.aclose()` (or `async with`).
    """
    return httpx.AsyncClient(
        follow_redirects=True,
        event_hooks={"request": [_refuse_private_targets]},
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        ),
    )
