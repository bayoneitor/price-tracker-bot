"""The notification-settings submenu: how and when alerts reach the user.

Muting, quiet hours, timezone, rate limit and digest mode existed only as typed
commands with a usage string — `/quiet_hours 22:00-08:00`, `/throttle 5` — which
meant knowing they existed and remembering the syntax. They are the settings most
worth having behind buttons, since the reason to reach for them is usually being
woken up by a notification.

The writes go through `settings.update_prefs`, so the menu and the commands share
one read-before-write path and cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from price_tracker.bot.decorators import _db
from price_tracker.bot.handlers.settings import (
    describe_digest,
    describe_mute,
    update_prefs,
)
from price_tracker.bot.keyboards import menu_exit_row, prompt_keyboard
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import set_pending
from price_tracker.notifier.preferences import PreferencesManager

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

# The callback tokens still say "delivery" — that is what this screen configures,
# and renaming them would only churn stored trails. The label says "settings"
# because that is what a reader recognises.
MENU = "menu_delivery"

# Offered mute durations, in hours; None is "until I turn it back on".
_MUTE_CHOICES: tuple[tuple[str, int | None], ...] = (
    ("mute_1", 1),
    ("mute_8", 8),
    ("mute_24", 24),
    ("mute_forever", None),
)


def _message_id(query: Any) -> int | None:
    return getattr(getattr(query, "message", None), "message_id", None)


async def handle_delivery_menu(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the notification-settings submenu (`menu_delivery`, `dlv_*`)."""
    if data == MENU:
        await _render(query, context, user_id)
        return True

    if not data.startswith("dlv_"):
        return False

    action = data.removeprefix("dlv_")
    repo = _db(context)

    if action == "unmute":
        await update_prefs(repo, user_id, mute=False, mute_until=None)
        await _render(query, context, user_id, notice=_("🔔 Notifications back on."))
        return True

    if action == "mute":
        rows = [
            [
                InlineKeyboardButton(
                    _("{hours}h").format(hours=hours), callback_data=f"dlv_{token}"
                )
                for token, hours in _MUTE_CHOICES
                if hours is not None
            ],
            [
                InlineKeyboardButton(
                    _("🔕 Until I turn it back on"), callback_data="dlv_mute_forever"
                )
            ],
        ]
        await query.edit_message_text(
            _("🔕 <b>Mute</b>\n\nFor how long?"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([*rows, menu_exit_row()]),
        )
        return True

    for token, hours in _MUTE_CHOICES:
        if action != token:
            continue
        until = None if hours is None else datetime.now(UTC) + timedelta(hours=hours)
        await update_prefs(repo, user_id, mute=True, mute_until=until)
        await _render(query, context, user_id, notice=describe_mute(None, until))
        return True

    if action == "digest":
        effective = await PreferencesManager(repo=repo).resolve_global(user_id=user_id)
        enabled = not effective.digest_mode
        await update_prefs(repo, user_id, digest_mode=enabled)
        await _render(
            query,
            context,
            user_id,
            notice=describe_digest(enabled, effective.digest_interval_minutes),
        )
        return True

    if action == "flush":
        flushed = await context.bot_data["digest_service"].flush_user(user_id=user_id)
        await _render(
            query,
            context,
            user_id,
            notice=_("📨 Flushed {count} pending digest entries.").format(count=flushed),
        )
        return True

    prompt = _PROMPTS.get(action)
    if prompt is None:
        return False
    set_pending(context, action, message=query.message)
    await query.edit_message_text(
        prompt(),
        parse_mode=ParseMode.HTML,
        reply_markup=prompt_keyboard(context, _message_id(query)),
    )
    return True


async def _render(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    notice: str | None = None,
) -> None:
    """Draw the panel from the *effective* preferences, not the stored row.

    A user with no row of their own still has settings — the defaults — and
    showing "not set" everywhere would be a lie.
    """
    repo = _db(context)
    prefs = await PreferencesManager(repo=repo).resolve_global(user_id=user_id)

    quiet = (
        f"{prefs.quiet_hours_start}–{prefs.quiet_hours_end}"
        if prefs.quiet_hours_start and prefs.quiet_hours_end
        else _("off")
    )
    lines = [
        _("⚙️ <b>Notification settings</b>"),
        "",
        _("🔕 Muted: {state}").format(state=_("yes") if prefs.mute else _("no")),
        _("🌙 Quiet hours: {window}").format(window=quiet),
        _("🌍 Timezone: {tz}").format(tz=prefs.timezone),
        _("🚦 Rate limit: {limit}").format(
            limit=prefs.throttle_per_hour if prefs.throttle_per_hour is not None else _("no cap")
        ),
        describe_digest(prefs.digest_mode, prefs.digest_interval_minutes),
    ]
    if notice:
        lines.extend(["", notice])

    mute_row = (
        [InlineKeyboardButton(_("🔔 Unmute"), callback_data="dlv_unmute")]
        if prefs.mute
        else [InlineKeyboardButton(_("🔕 Mute"), callback_data="dlv_mute")]
    )
    rows = [
        mute_row,
        [
            InlineKeyboardButton(_("🌙 Quiet hours"), callback_data="dlv_quiet_hours"),
            InlineKeyboardButton(_("🌍 Timezone"), callback_data="dlv_timezone"),
        ],
        [
            InlineKeyboardButton(_("🚦 Rate limit"), callback_data="dlv_throttle"),
            InlineKeyboardButton(
                _("📬 Instant") if prefs.digest_mode else _("📥 Digest"),
                callback_data="dlv_digest",
            ),
        ],
        [
            InlineKeyboardButton(_("⏱ Digest interval"), callback_data="dlv_digest_interval"),
            InlineKeyboardButton(_("📨 Send digest now"), callback_data="dlv_flush"),
        ],
        menu_exit_row(),
    ]
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# Built per call so the locale is the reader's, not whichever was active at import.
_PROMPTS = {
    "quiet_hours": lambda: _(
        "🌙 <b>Quiet hours</b>\n\n"
        "Type a window like <code>22:00-08:00</code>, or <code>off</code> to disable it."
    ),
    "timezone": lambda: _(
        "🌍 <b>Timezone</b>\n\nType an IANA name, e.g. <code>Europe/Madrid</code>."
    ),
    "throttle": lambda: _(
        "🚦 <b>Rate limit</b>\n\n"
        "Type the most alerts you want in an hour, or <code>off</code> for no cap."
    ),
    "digest_interval": lambda: _(
        "⏱ <b>Digest interval</b>\n\nType how many minutes between digests, e.g. <code>60</code>."
    ),
}
