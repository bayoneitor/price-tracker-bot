"""Telegram command menu — the list behind the blue Menu button.

Registered on startup via ``setMyCommands`` so the client shows a tappable,
described command list instead of requiring users to remember command names.

Three things this deliberately does:

- Publishes only the English command names. Every command also has an Italian
  alias (``/aggiungi``, ``/soglia``, …) kept for compatibility; listing both
  would double the menu with duplicate entries.
- Sends one list per supported locale using ``language_code``, so a client set
  to Italian or Spanish sees translated descriptions. Telegram picks the entry
  matching the viewer's client language and falls back to the unscoped list.
- Scopes admin commands to the admins' own chats, so ordinary users are not
  shown commands that would reject them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import (
    BotCommand,
    BotCommandScope,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from telegram.error import TelegramError

from price_tracker.bot.messages import N_, _, reset_locale, set_locale

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from telegram import Bot

logger = logging.getLogger(__name__)

# (command, description msgid). Order is what the user sees — most-used first,
# not alphabetical. Descriptions are marked with N_() and translated at publish
# time, once per locale.
USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("menu", N_("Open the main menu")),
    ("list", N_("List your tracked products")),
    ("add", N_("Track a new product by URL")),
    ("check", N_("Check one product's price now")),
    ("checkall", N_("Check every product now")),
    ("history", N_("Price history chart")),
    ("target", N_("Set a target price")),
    ("threshold", N_("Set the price-drop threshold")),
    ("refresh", N_("Set how often a product is checked")),
    ("pause", N_("Pause tracking for a product")),
    ("reactivate", N_("Resume a paused product")),
    ("delete", N_("Delete a tracked product")),
    ("reset", N_("Reset the base price to the current one")),
    ("status", N_("Your stats and check interval")),
    ("errors", N_("Products with recent read errors")),
    ("prefs", N_("Show your notification settings")),
    ("mute", N_("Mute notifications")),
    ("unmute", N_("Unmute notifications")),
    ("quiet_hours", N_("Set quiet hours")),
    ("timezone", N_("Set your timezone")),
    ("throttle", N_("Limit how often you are notified")),
    ("digest_mode", N_("Switch between instant and digest delivery")),
    ("digest_now", N_("Send the pending digest now")),
    ("export", N_("Export your products as CSV")),
    ("import", N_("Import products from a CSV file")),
    ("help", N_("Show the menu and available commands")),
)

ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("users", N_("List authorized users")),
    ("adduser", N_("Authorize a Telegram user")),
    ("removeuser", N_("Revoke a user's access")),
    ("nick", N_("Set a nickname for a user")),
    ("setinterval", N_("Set the global check interval")),
    ("health", N_("Scraper health and quarantine report")),
    ("debug", N_("Inspect what a scraper reads from a URL")),
)

# Telegram matches `language_code` against the viewer's client language, which
# is a 2-letter code; the catalogs resolve "it"/"es" to it_IT/es_ES.
MENU_LANGUAGES: tuple[str, ...] = ("en", "it", "es")


def _render(commands: Iterable[tuple[str, str]], lang: str) -> list[BotCommand]:
    """Translate `commands` into BotCommand objects under `lang`."""
    token = set_locale(lang)
    try:
        return [BotCommand(name, _(description)) for name, description in commands]
    finally:
        reset_locale(token)


async def publish_command_menu(
    bot: Bot,
    *,
    admin_ids: Sequence[int] = (),
    default_language: str = "en",
) -> None:
    """Publish the command menu to Telegram, per locale and per scope.

    Best-effort: a failure here must never stop the bot from starting, so
    Telegram errors are logged and swallowed. The commands themselves keep
    working regardless — the menu is discovery, not dispatch.
    """
    private_chats = BotCommandScopeAllPrivateChats()

    # Unscoped list first: the fallback for clients whose language has no entry.
    await _publish(bot, _render(USER_COMMANDS, default_language), scope=private_chats)
    for lang in MENU_LANGUAGES:
        await _publish(bot, _render(USER_COMMANDS, lang), scope=private_chats, language_code=lang)

    admin_menu = (*USER_COMMANDS, *ADMIN_COMMANDS)
    for admin_id in admin_ids:
        scope = BotCommandScopeChat(chat_id=admin_id)
        await _publish(bot, _render(admin_menu, default_language), scope=scope)
        for lang in MENU_LANGUAGES:
            await _publish(bot, _render(admin_menu, lang), scope=scope, language_code=lang)


async def _publish(
    bot: Bot,
    commands: list[BotCommand],
    *,
    scope: BotCommandScope,
    language_code: str | None = None,
) -> None:
    """One setMyCommands call, with failures logged rather than raised."""
    try:
        await bot.set_my_commands(commands, scope=scope, language_code=language_code)
    except TelegramError as exc:
        logger.warning(
            "Could not publish command menu (scope=%s, language=%s): %s",
            type(scope).__name__,
            language_code or "default",
            exc,
        )
