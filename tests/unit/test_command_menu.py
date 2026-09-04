"""Command-menu registration (Telegram's Menu button).

Covers the contract that makes the menu usable: every published command is a
real handler, admin commands stay scoped to admins, descriptions are
translated per locale, and a Telegram outage never blocks startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.error import TelegramError

from price_tracker.bot.commands import (
    ADMIN_COMMANDS,
    MENU_LANGUAGES,
    USER_COMMANDS,
    publish_command_menu,
)

if TYPE_CHECKING:
    from telegram import BotCommand


def _registered_command_names() -> set[str]:
    """Every command name the bot actually registers a handler for."""
    from telegram.ext import Application, CommandHandler

    from price_tracker.bot.handlers import register_handlers

    app = Application.builder().token("1:dummy").build()
    register_handlers(app)
    names: set[str] = set()
    for handlers in app.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                names.update(handler.commands)
    return names


def test_every_published_command_has_a_handler() -> None:
    """A menu entry with no handler is a dead button — the user taps and nothing happens."""
    registered = _registered_command_names()
    published = {name for name, _ in (*USER_COMMANDS, *ADMIN_COMMANDS)}
    missing = sorted(published - registered)
    assert not missing, f"published but not registered: {missing}"


def test_published_command_names_are_valid_for_telegram() -> None:
    """Telegram rejects names outside [a-z0-9_]{1,32}."""
    for name, description in (*USER_COMMANDS, *ADMIN_COMMANDS):
        assert 1 <= len(name) <= 32, name
        assert name.islower(), name
        assert all(c.isalnum() or c == "_" for c in name), name
        assert 1 <= len(description) <= 256, name


def test_no_duplicate_or_italian_alias_entries() -> None:
    """Only the English names are published; aliases would double the menu."""
    names = [name for name, _ in (*USER_COMMANDS, *ADMIN_COMMANDS)]
    assert len(names) == len(set(names)), "duplicate entries"
    italian_aliases = {
        "aggiungi",
        "lista",
        "elimina",
        "controlla",
        "storia",
        "azzera",
        "pausa",
        "riattiva",
        "esporta",
        "importa",
        "soglia",
        "stato",
        "errori",
        "utenti",
        "intervallo",
    }
    assert not (set(names) & italian_aliases)


def test_admin_commands_are_not_in_the_public_menu() -> None:
    """An ordinary user must not be shown commands that would reject them."""
    user_names = {name for name, _ in USER_COMMANDS}
    admin_names = {name for name, _ in ADMIN_COMMANDS}
    assert not (user_names & admin_names)


@pytest.mark.asyncio
async def test_publish_sets_public_and_per_language_menus() -> None:
    bot = AsyncMock()
    await publish_command_menu(bot, admin_ids=(), default_language="en")

    calls = bot.set_my_commands.await_args_list
    # One unscoped fallback + one per language.
    assert len(calls) == 1 + len(MENU_LANGUAGES)

    languages = [call.kwargs["language_code"] for call in calls]
    assert languages[0] is None
    assert set(languages[1:]) == set(MENU_LANGUAGES)
    for call in calls:
        assert isinstance(call.kwargs["scope"], BotCommandScopeAllPrivateChats)


@pytest.mark.asyncio
async def test_publish_scopes_admin_menu_to_admin_chats() -> None:
    bot = AsyncMock()
    await publish_command_menu(bot, admin_ids=(42, 77), default_language="en")

    chat_calls = [
        call
        for call in bot.set_my_commands.await_args_list
        if isinstance(call.kwargs["scope"], BotCommandScopeChat)
    ]
    assert {call.kwargs["scope"].chat_id for call in chat_calls} == {42, 77}

    admin_names = {name for name, _ in ADMIN_COMMANDS}
    for call in chat_calls:
        published: list[BotCommand] = call.args[0]
        names = {command.command for command in published}
        assert admin_names <= names, "admin chat menu must include the admin commands"


@pytest.mark.asyncio
async def test_descriptions_are_translated_per_language() -> None:
    """The whole point of language_code: an Italian client reads Italian."""
    bot = AsyncMock()
    await publish_command_menu(bot, admin_ids=(), default_language="en")

    by_language = {
        call.kwargs["language_code"]: {c.command: c.description for c in call.args[0]}
        for call in bot.set_my_commands.await_args_list
    }
    assert by_language["en"]["menu"] == "Open the main menu"
    assert by_language["it"]["menu"] == "Apri il menu principale"
    assert by_language["es"]["menu"] == "Abre el menú principal"


@pytest.mark.asyncio
async def test_publish_survives_telegram_errors() -> None:
    """A failed menu publish must not stop the bot from starting."""
    bot = AsyncMock()
    bot.set_my_commands.side_effect = TelegramError("bad gateway")

    await publish_command_menu(bot, admin_ids=(42,), default_language="en")

    assert bot.set_my_commands.await_count > 0


@pytest.mark.asyncio
async def test_publish_leaves_the_caller_locale_untouched() -> None:
    """Rendering each language must not leak out of publish_command_menu."""
    from price_tracker.bot.messages import _, get_translation, reset_locale, set_locale

    get_translation.cache_clear()
    token = set_locale("es")
    try:
        await publish_command_menu(AsyncMock(), admin_ids=(), default_language="en")
        assert _("Open the main menu") == "Abre el menú principal"
    finally:
        reset_locale(token)
        get_translation.cache_clear()
