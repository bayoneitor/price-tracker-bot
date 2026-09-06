"""The four inline admin prompts must not look up a product first.

`handle_text_input` resolved `_get_user_product(context, product_id, user_id)` for
every prompt before branching on which one it was. Three of the admin prompts carry
no product id at all (the slot holds 0) and `admin_nick` carries a *user* id, so the
lookup found nothing and all four answered "❌ Product not found." and did nothing —
Menu → Admin → Add user / Nickname / Global interval / Scraper debug were dead.

The product lookup now happens only for the prompts whose `PendingSpec` asks for one.
Every test here leaves `db.get_product` returning None, which is what production does.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from price_tracker.bot.handlers.text_input import handle_text_input
from price_tracker.bot.navigation import PendingInput
from price_tracker.config import Config

ADMIN_ID = 1
TARGET_USER_ID = 987654321


def _config() -> Config:
    return Config(
        telegram_bot_token="x",
        admin_users=(),
        check_interval_minutes=360,
        database_path=":memory:",
        default_threshold_type="percentage",
        default_threshold_value="10",
        max_consecutive_errors=10,
        check_delay_seconds=0.0,
        notification_cooldown_hours=24,
        request_timeout=30,
        log_level="INFO",
        lang="en",
    )


def _update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.effective_user.language_code = "en"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _context(pending: PendingInput) -> MagicMock:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=True)
    db.update_user_info = AsyncMock()
    # Production reality: there is no product with id 0, nor one keyed by a user id.
    db.get_product = AsyncMock(return_value=None)
    db.get_product_for_user = AsyncMock(return_value=None)
    db.get_user = AsyncMock(return_value=None)
    context = MagicMock()
    context.user_data = {"pending_action": pending}
    context.bot_data = {"db": db, "config": _config()}
    context.bot.send_message = AsyncMock()
    job_queue = MagicMock()
    job_queue.get_jobs_by_name.return_value = []
    context.job_queue = job_queue
    return context


def _replies(update: MagicMock) -> str:
    return " ".join(str(call.args[0]) for call in update.message.reply_text.await_args_list)


@pytest.mark.asyncio
async def test_add_user_authorizes_instead_of_reporting_a_missing_product() -> None:
    context = _context(PendingInput("admin_adduser"))
    update = _update(str(TARGET_USER_ID))

    await handle_text_input(update, context)

    context.bot_data["db"].add_user.assert_awaited_once_with(TARGET_USER_ID, is_admin=False)
    assert "Product not found" not in _replies(update)


@pytest.mark.asyncio
async def test_nickname_renames_the_user_the_prompt_was_opened_for() -> None:
    context = _context(PendingInput("admin_nick", TARGET_USER_ID))
    update = _update("Alice")

    await handle_text_input(update, context)

    context.bot_data["db"].update_user_info.assert_any_await(TARGET_USER_ID, display_name="Alice")
    assert "Product not found" not in _replies(update)


@pytest.mark.asyncio
async def test_global_interval_is_stored_and_rescheduled() -> None:
    context = _context(PendingInput("admin_interval"))
    update = _update("120")

    await handle_text_input(update, context)

    context.bot_data["db"].set_config.assert_awaited_once_with("check_interval_minutes", "120")
    context.job_queue.run_repeating.assert_called_once()
    assert "Product not found" not in _replies(update)


@pytest.mark.asyncio
async def test_scraper_debug_runs_the_report() -> None:
    context = _context(PendingInput("admin_debug"))
    update = _update("https://example.com/p/1")

    with patch("price_tracker.bot.handlers.debug.cmd_debug", new=AsyncMock()) as debug:
        await handle_text_input(update, context)

    debug.assert_awaited_once()
    assert "Product not found" not in _replies(update)


@pytest.mark.asyncio
async def test_a_non_admin_holding_an_admin_prompt_is_refused() -> None:
    """Defence in depth: the button that arms these is admin-gated, the answer is too."""
    context = _context(PendingInput("admin_adduser"))
    context.bot_data["db"].is_user_admin = AsyncMock(return_value=False)
    update = _update(str(TARGET_USER_ID))

    await handle_text_input(update, context)

    context.bot_data["db"].add_user.assert_not_awaited()
    assert "pending_action" not in context.user_data


@pytest.mark.asyncio
async def test_a_prompt_that_does_need_a_product_still_checks_ownership() -> None:
    context = _context(PendingInput("target", 42))
    update = _update("29.99")

    await handle_text_input(update, context)

    context.bot_data["db"].set_target_price.assert_not_awaited()
    assert "Product not found" in _replies(update)
    assert "pending_action" not in context.user_data
