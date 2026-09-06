"""The SSRF boundary has to hold where the URL leads, not only where it starts.

`validate_public_url` guards a URL at the point it is stored. It cannot guard a
redirect: the shared client follows them, so a public host answering
`302 → http://169.254.169.254/` was fetched anyway — the code said as much, in a
note on the guard's own docstring.

`/debug` had no guard at all, and reaches curl_cffi and Scrapling directly, which
never touch the shared client.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from price_tracker.core import http_client
from price_tracker.core.url_utils import UnsafeURLError

METADATA = "http://169.254.169.254/latest/meta-data/"


# ── The hook itself ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [METADATA, "http://127.0.0.1/", "http://localhost/admin", "http://[::1]/", "http://10.0.0.1/"],
)
async def test_a_private_target_is_refused(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await http_client._refuse_private_targets(httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_an_unresolvable_host_is_allowed_through() -> None:
    """It cannot be connected to, so it carries no risk; the scrape just fails."""
    await http_client._refuse_private_targets(httpx.Request("GET", "http://nothing.invalid/p/1"))


# ── Through the client, across a redirect ────────────────────────────────


def _allow_only(*hosts: str) -> Any:
    """Stand in for DNS: these hosts are public, everything else is not."""

    def check(url: str) -> None:
        if httpx.URL(url).host not in hosts:
            raise UnsafeURLError(f"host {httpx.URL(url).host} is a non-public address")

    return check


@pytest.mark.asyncio
@respx.mock
async def test_a_redirect_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first hop looks fine, which is the whole point of the vector."""
    monkeypatch.setattr(http_client, "validate_public_url", _allow_only("shop.example"))
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(302, headers={"Location": METADATA})
    )
    metadata = respx.get(METADATA).mock(return_value=httpx.Response(200, text="secrets"))

    async with http_client.build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/p/1")

    assert not metadata.called, "the private hop was still fetched"


@pytest.mark.asyncio
@respx.mock
async def test_an_ordinary_redirect_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not break a shop that redirects to its canonical URL."""
    monkeypatch.setattr(http_client, "validate_public_url", _allow_only("shop.example"))
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(301, headers={"Location": "https://shop.example/p/1/"})
    )
    respx.get("https://shop.example/p/1/").mock(return_value=httpx.Response(200, text="ok"))

    async with http_client.build_client() as client:
        response = await client.get("https://shop.example/p/1")

    assert response.text == "ok"


# ── /debug, which reaches past the shared client ─────────────────────────


@pytest.mark.asyncio
async def test_debug_refuses_a_private_url_before_fetching_anything() -> None:
    from price_tracker.bot.handlers.debug import cmd_debug

    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=True)
    client = AsyncMock()
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = [METADATA]
    context.bot_data = {"db": db, "http_client": client, "scraper": MagicMock()}

    await cmd_debug(update, context)

    client.get.assert_not_called()
    assert "not allowed" in update.message.reply_text.await_args.args[0]
