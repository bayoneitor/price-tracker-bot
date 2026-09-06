"""Settings handler: /intervallo (admin global check interval).

Ported from monolithic bot.py [Task 17].

Plan 2 F3.D additions [Task 29]: per-user notification preference commands
(/mute /unmute /digest_mode /quiet_hours /timezone /throttle /prefs /digest_now).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import available_timezones

from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import _config, _db, admin_only, restricted, with_locale
from price_tracker.bot.handlers._helpers import _format_minutes
from price_tracker.bot.messages import _
from price_tracker.db.models import NotificationPrefs

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


# Cache the (large) IANA timezone set once at import time — building this on
# every /timezone invocation walks the zoneinfo dir tree unnecessarily.
_VALID_TIMEZONES: frozenset[str] = frozenset(available_timezones())


@with_locale
@admin_only
async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the global price-check interval (admin)."""
    if not context.args:
        config = _config(context)
        await update.message.reply_text(
            _(
                "⏱ Current interval: <b>every {minutes} minutes</b>\n\n"
                "Usage: /setinterval &lt;minutes&gt;\n"
                "Example: <code>/setinterval 120</code> for every 2 hours"
            ).format(minutes=config.check_interval_minutes),
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text(_("❌ Invalid value."))
        return
    if minutes < 5:
        await update.message.reply_text(_("❌ The minimum interval is 5 minutes."))
        return
    if minutes > 1440 * 7:
        await update.message.reply_text(_("❌ The maximum interval is 7 days."))
        return

    await _db(context).set_config("check_interval_minutes", str(minutes))
    _reschedule_periodic_check(context, minutes)

    await update.message.reply_text(
        _("✅ Interval updated: <b>every {interval}</b>").format(interval=_format_minutes(minutes)),
        parse_mode=ParseMode.HTML,
    )


async def update_prefs(
    repo: Any, user_id: int, *, product_id: int | None = None, **changes: Any
) -> NotificationPrefs:
    """Read-before-write update of one notification-preferences row.

    ``upsert_notification_prefs`` does a full-row UPDATE, so writing one field
    without reading first silently resets the others — muting would clear the
    user's timezone and digest settings. Every command here had its own copy of
    this dance; this is the one copy.
    """
    existing = await repo.get_notification_prefs(user_id=user_id, product_id=product_id)
    if existing is not None:
        prefs = dataclasses.replace(existing, **changes)
    else:
        prefs = NotificationPrefs(user_id=user_id, product_id=product_id, **changes)
    await repo.upsert_notification_prefs(prefs)
    return prefs


# ── Rendering, shared by the commands and the notification-settings menu ──


def describe_mute(product_id: int | None, mute_until: datetime | None) -> str:
    """One line saying what is muted and until when."""
    scope = _("all products") if product_id is None else _("product {pid}").format(pid=product_id)
    when = (
        _("until further notice")
        if mute_until is None
        else _("until {when}").format(when=mute_until.strftime("%Y-%m-%d %H:%M UTC"))
    )
    return _("🔕 Muted {scope} {when}.").format(scope=scope, when=when)


def describe_digest(enabled: bool, interval: int) -> str:
    """One line saying how alerts are delivered."""
    if not enabled:
        return _("📬 Alerts: as they happen.")
    return _("📥 Alerts: digest every {minutes} min.").format(minutes=interval)


def describe_throttle(limit: int | None) -> str:
    """One line saying how many alerts an hour are allowed through."""
    if limit is None:
        return _("🚦 Rate limit removed — no cap on alerts.")
    return _("🚦 At most {count} alerts per hour.").format(count=limit)


# ── Plan 2 F3.D: notification preference commands ────────────────────


def _reschedule_periodic_check(context: ContextTypes.DEFAULT_TYPE, minutes: int) -> None:
    """Reschedule the periodic price-check job so a new interval takes effect live.

    Config is frozen and the job interval is fixed at startup, so a runtime change
    must remove the existing job and re-add it at the new cadence.
    """
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None:
        return
    from price_tracker.main import scheduled_check_job  # noqa: PLC0415 — avoid import cycle

    for job in job_queue.get_jobs_by_name("periodic_check"):
        job.schedule_removal()
    job_queue.run_repeating(
        scheduled_check_job, interval=minutes * 60, first=minutes * 60, name="periodic_check"
    )


def _valid_hhmm(value: str) -> bool:
    """Return True if ``value`` is a 24h ``HH:MM`` time string."""
    if len(value) != 5 or value[2] != ":":
        return False
    try:
        h, m = int(value[:2]), int(value[3:])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


@with_locale
@restricted
async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/mute [product_id|all] [hours|forever]`` (default: all 24h)."""
    repo = _db(context)
    args = context.args or []
    target = args[0] if args else "all"
    duration = args[1] if len(args) > 1 else "24"

    product_id: int | None
    if target == "all":
        product_id = None
    else:
        try:
            product_id = int(target)
        except ValueError:
            await update.message.reply_text(_("Usage: /mute [product_id|all] [hours|forever]"))
            return

    mute_until: datetime | None
    if duration == "forever":
        mute_until = None
    else:
        try:
            hours = int(duration)
        except ValueError:
            await update.message.reply_text(_("Duration must be a number of hours or 'forever'"))
            return
        if hours <= 0:
            await update.message.reply_text(
                _("Duration must be a positive number of hours or 'forever'")
            )
            return
        mute_until = datetime.now(UTC) + timedelta(hours=hours)

    await update_prefs(
        repo, update.effective_user.id, product_id=product_id, mute=True, mute_until=mute_until
    )
    await update.message.reply_text(describe_mute(product_id, mute_until))


@with_locale
@restricted
async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/unmute [product_id|all]``."""
    repo = _db(context)
    args = context.args or []
    target = args[0] if args else "all"
    product_id: int | None = None
    if target != "all":
        try:
            product_id = int(target)
        except ValueError:
            await update.message.reply_text(_("Usage: /unmute [product_id|all]"))
            return
    await update_prefs(
        repo, update.effective_user.id, product_id=product_id, mute=False, mute_until=None
    )
    await update.message.reply_text(_("🔔 Notifications back on."))


@with_locale
@restricted
async def digest_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/digest_mode on|off [interval_min]``."""
    repo = _db(context)
    args = context.args or []
    if not args or args[0] not in ("on", "off"):
        await update.message.reply_text(_("Usage: /digest_mode on|off [interval_min]"))
        return
    enabled = args[0] == "on"
    interval = 60
    if enabled and len(args) > 1:
        try:
            interval = int(args[1])
        except ValueError:
            await update.message.reply_text(_("interval_min must be a positive integer"))
            return
        if interval <= 0:
            await update.message.reply_text(_("interval_min must be > 0"))
            return

    await update_prefs(
        repo,
        update.effective_user.id,
        digest_mode=enabled,
        digest_interval_minutes=interval,
    )
    await update.message.reply_text(describe_digest(enabled, interval))


@with_locale
@restricted
async def quiet_hours_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/quiet_hours HH:MM-HH:MM`` or ``/quiet_hours off``."""
    repo = _db(context)
    args = context.args or []
    if not args:
        await update.message.reply_text(_("Usage: /quiet_hours HH:MM-HH:MM | off"))
        return
    user_id = update.effective_user.id

    if args[0] == "off":
        await update_prefs(repo, user_id, quiet_hours_start=None, quiet_hours_end=None)
        await update.message.reply_text(_("🌙 Quiet hours disabled."))
        return

    spec = args[0]
    if "-" not in spec:
        await update.message.reply_text(_("Format: HH:MM-HH:MM (e.g. 22:00-08:00)"))
        return
    start, end = spec.split("-", 1)
    if not _valid_hhmm(start) or not _valid_hhmm(end):
        await update.message.reply_text(_("Invalid time format. Use 24h HH:MM."))
        return
    if start == end:
        await update.message.reply_text(_("Quiet hours start and end cannot be the same time"))
        return
    await update_prefs(repo, user_id, quiet_hours_start=start, quiet_hours_end=end)
    await update.message.reply_text(
        _("🌙 Quiet hours: {start}–{end}.").format(start=start, end=end)
    )


@with_locale
@restricted
async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/timezone <TZ>`` (e.g. ``Europe/Berlin``)."""
    repo = _db(context)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            _("Usage: /timezone &lt;TZ name&gt;"), parse_mode=ParseMode.HTML
        )
        return
    tz = args[0]
    if tz not in _VALID_TIMEZONES:
        await update.message.reply_text(_("Unknown timezone: {tz}").format(tz=tz))
        return
    await update_prefs(repo, update.effective_user.id, timezone=tz)
    await update.message.reply_text(_("🌍 Timezone set to {tz}.").format(tz=tz))


@with_locale
@restricted
async def throttle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/throttle <N>`` or ``/throttle off``."""
    repo = _db(context)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            _("Usage: /throttle &lt;N&gt; | off"), parse_mode=ParseMode.HTML
        )
        return
    limit: int | None = None
    if args[0] != "off":
        try:
            limit = int(args[0])
        except ValueError:
            await update.message.reply_text(_("N must be a positive integer"))
            return
        if limit <= 0:
            await update.message.reply_text(_("N must be > 0"))
            return
    await update_prefs(repo, update.effective_user.id, throttle_per_hour=limit)
    await update.message.reply_text(describe_throttle(limit))


@with_locale
@restricted
async def prefs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/prefs [product_id]`` — render resolved (effective) preferences."""
    from price_tracker.notifier.preferences import PreferencesManager

    repo = _db(context)
    args = context.args or []
    user_id = update.effective_user.id
    product_id: int | None = None
    if args:
        try:
            product_id = int(args[0])
        except ValueError:
            await update.message.reply_text(_("product_id must be an integer"))
            return
        if product_id <= 0:
            await update.message.reply_text(_("product_id must be a positive integer"))
            return
    prefs_mgr = PreferencesManager(repo=repo)
    eff = await prefs_mgr.resolve(user_id=user_id, product_id=product_id or 0)
    scope = f"product {product_id}" if product_id else "global"
    lines = [
        "<b>Effective preferences</b>",
        f"  scope: {scope}",
        f"  mute: {eff.mute}",
        f"  digest_mode: {eff.digest_mode} (interval {eff.digest_interval_minutes}m)",
        f"  quiet_hours: {eff.quiet_hours_start or '—'}–{eff.quiet_hours_end or '—'}",
        f"  throttle_per_hour: {eff.throttle_per_hour or 'unlimited'}",
        f"  timezone: {eff.timezone}",
    ]
    await update.message.reply_html("\n".join(lines))


@with_locale
@restricted
async def digest_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: ``/digest_now`` — flush pending digest entries immediately."""
    digest_svc = context.bot_data["digest_service"]
    user_id = update.effective_user.id
    flushed = await digest_svc.flush_user(user_id=user_id)
    await update.message.reply_text(
        _("📨 Flushed {count} pending digest entries.").format(count=flushed)
    )


def register(app: Application) -> None:
    """Register settings command handlers on ``app``."""
    app.add_handler(CommandHandler("intervallo", cmd_set_interval))
    app.add_handler(CommandHandler("setinterval", cmd_set_interval))
    # Plan 2 F3.D notification preference commands [Task 29]
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("digest_mode", digest_mode_command))
    app.add_handler(CommandHandler("quiet_hours", quiet_hours_command))
    app.add_handler(CommandHandler("timezone", timezone_command))
    app.add_handler(CommandHandler("throttle", throttle_command))
    app.add_handler(CommandHandler("prefs", prefs_command))
    app.add_handler(CommandHandler("digest_now", digest_now_command))
