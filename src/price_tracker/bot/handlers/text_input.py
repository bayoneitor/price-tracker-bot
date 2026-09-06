"""Plain-text intake: one router for pasted links, prompt answers and list jumps.

There used to be two handlers here — `handle_url`, filtered on a URL regex, and
`handle_text_input` for everything else — registered in that order and both in
python-telegram-bot's default group, where only the first match runs. So a URL
was *always* read as "track this product", even while the bot was waiting for the
user to paste a URL for something else, which left `/menu → Admin → Scraper debug`
unusable: it asked for a link and tracked it instead of analysing it.

Ordering can't express "unless something else is pending", so there is now a
single handler that asks the question in the right order: an open prompt wins,
then a pasted link, then a bare number steering an open `/list`.

An answer is shown by editing the message that asked for it, and the typed
answer itself is deleted: a three-message exchange (question, answer,
confirmation) collapses to the one message the user is looking at, and a
rejected value no longer leaves a trail of failed attempts behind.
"""

from __future__ import annotations

import contextlib
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from price_tracker.bot.decorators import _config, _convert_display, _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_minutes,
    _format_threshold,
    _get_user_product,
    _parse_threshold_input,
    _safe_dec,
)
from price_tracker.bot.handlers.settings import _reschedule_periodic_check, update_prefs
from price_tracker.bot.keyboards import cancel_button, nav_row
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import PendingInput, clear_pending, get_pending

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Sentinel answers that dismiss a prompt. Superseded by /cancel and the ✖ button,
# kept so the words people already type keep working.
_CANCEL_WORDS = frozenset({"no", "skip", "salta", "annulla", "cancel", "cancelar", "-"})


class _Retry(Exception):  # noqa: N818 — a control-flow signal, not an error condition
    """The answer was not usable: say why and leave the prompt open to try again."""


# ── Router ────────────────────────────────────────────────────────


@with_locale
@restricted
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a plain-text message to whatever it is actually answering."""
    from price_tracker.bot.handlers.product import URL_PATTERN, _add_product  # noqa: PLC0415

    text = (update.message.text or "").strip()

    pending = get_pending(context)
    if pending is not None:
        await _answer_prompt(update, context, pending, text)
        return

    match = URL_PATTERN.search(text)
    if match:
        await _add_product(update, context, match.group(0).rstrip(".,;:!?)"))
        return

    # A bare number steers an open /list instead of being dropped.
    await _try_list_jump(update, context, text)


async def _answer_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: PendingInput, text: str
) -> None:
    """Feed `text` to the open prompt, keeping it open if the answer is unusable."""
    from price_tracker.bot.handlers.product import URL_PATTERN  # noqa: PLC0415

    if text.lower() in _CANCEL_WORDS:
        clear_pending(context)
        await _show(update, context, pending, _("👍 Cancelled — nothing was changed."))
        return

    spec = pending.spec
    if not spec.accepts_url and URL_PATTERN.search(text):
        # The prompt stays open: the user almost certainly meant to answer it, and
        # silently tracking the link is what made `/debug` unreachable before.
        await _show(
            update,
            context,
            pending,
            _(
                "⏳ I am still waiting for {what}.\n"
                "Send /cancel first if you wanted to track that link instead."
            ).format(what=_(spec.label)),
            keep_open=True,
        )
        return

    user_id = update.effective_user.id
    product: dict[str, Any] | None = None
    if spec.needs_product:
        product = await _get_user_product(context, pending.target_id, user_id)
        if not product:
            clear_pending(context)
            await _show(update, context, pending, _("❌ Product not found."))
            return
    elif pending.action.startswith("admin_") and not await _db(context).is_user_admin(user_id):
        clear_pending(context)
        await _show(update, context, pending, _("⛔ Admin-only command."))
        return

    try:
        outcome = await _ACTIONS[pending.action](update, context, pending, text, product)
    except _Retry as retry:
        # Leave the prompt armed so the next message is read as another attempt.
        await _show(update, context, pending, str(retry), keep_open=True)
        return
    clear_pending(context)
    if outcome is None:
        # The action drew a whole screen into the prompt rather than a line of
        # text; there is nothing left to render, only the typing to clear away.
        await _drop_typed_answer(update)
        return
    await _show(update, context, pending, outcome)


async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    *,
    keep_open: bool = False,
) -> None:
    """Report the outcome in the message that asked, and drop the typed answer.

    Falls back to a plain reply when the question is gone — too old to edit, or
    deleted by the user — because losing the answer entirely would be worse than
    one extra message. The typed answer is only deleted once the outcome is
    actually on screen somewhere.
    """
    row = (
        [cancel_button()] if keep_open else nav_row(context, pending.prompt_message_id, close=True)
    )
    markup = InlineKeyboardMarkup([row]) if row else None

    edited = False
    if pending.prompt_message_id is not None and pending.chat_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=pending.chat_id,
                message_id=pending.prompt_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            edited = True
        except TelegramError as exc:
            logger.debug("Could not edit the prompt, replying instead: %s", exc)

    if not edited:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    await _drop_typed_answer(update)


async def _drop_typed_answer(update: Update) -> None:
    """Remove the user's message once its outcome is on screen somewhere else.

    Bots may delete incoming messages in private chats; if this one cannot, the
    answer simply stays visible.
    """
    with contextlib.suppress(TelegramError):
        await update.message.delete()


async def _try_list_jump(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Jump an open /list to the index the user typed. Returns True if handled."""
    from price_tracker.bot.handlers.product_list import (  # noqa: PLC0415
        LIST_MESSAGE_KEY,
        build_list_view,
    )

    message_id = context.user_data.get(LIST_MESSAGE_KEY)
    if message_id is None or not text.isdigit():
        return False

    products = await _db(context).get_active_products(update.effective_user.id)
    if not products:
        return False

    # Users type the 1-based number they see in the index.
    position = int(text) - 1
    if not 0 <= position < len(products):
        await update.message.reply_text(
            _("❌ No product {n} — the list has {count}.").format(n=text, count=len(products))
        )
        return True

    view_text, keyboard = build_list_view(products, position)
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=view_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        # The listing was deleted or is too old to edit — drop the stale
        # reference so later numbers are not silently swallowed.
        logger.debug("Could not steer the open listing: %s", exc)
        context.user_data.pop(LIST_MESSAGE_KEY, None)
        return False
    return True


# ── One function per prompt ───────────────────────────────────────
# Each receives the resolved product when its spec asks for one, returns the text
# to show, and raises `_Retry` when the answer cannot be used. Returning is what
# closes the prompt; raising keeps it open for another attempt.


def _product_name(product: dict[str, Any] | None) -> str:
    return ((product or {}).get("name") or _("Unknown"))[:60]


async def _do_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set or clear a product's target price."""
    try:
        target = Decimal(text.replace(",", ".").replace("€", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise _Retry(_("❌ Invalid price. Try again.")) from exc

    db = _db(context)
    product_id = pending.target_id
    if target <= 0:
        await db.set_target_price(product_id, None)
        return _("🎯 Target cleared for #{pid}.").format(pid=product_id)

    await db.set_target_price(product_id, target)
    assert product is not None
    current = _safe_dec(product.get("current_price"))
    currency = product.get("currency", "EUR")
    lines = _("🎯 Target: <b>{target}</b>\n📦 {name}").format(
        target=_convert_display(target, currency), name=_escape_html(_product_name(product))
    )
    if current and target < current:
        diff_pct = ((current - target) / current) * 100
        lines += _("\n💰 Current: {price} (-{pct:.1f}% needed)").format(
            price=_convert_display(current, currency), pct=diff_pct
        )
    return lines


async def _do_threshold(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set a product's price-drop threshold."""
    try:
        threshold_type, threshold_value = _parse_threshold_input(text)
    except ValueError as exc:
        raise _Retry(_("❌ Invalid value. Try again (e.g. 20% or 50).")) from exc

    await _db(context).set_threshold(pending.target_id, threshold_type, threshold_value)
    return _("🎯 Threshold: <b>{threshold}</b>\n📦 {name}").format(
        threshold=_format_threshold(threshold_type, threshold_value),
        name=_escape_html(_product_name(product)),
    )


async def _do_refresh(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set a product's own check interval, or hand it back to the global one."""
    try:
        minutes = int(text.strip())
    except ValueError as exc:
        raise _Retry(_("❌ Invalid number. Try again.")) from exc

    db = _db(context)
    name = _escape_html(_product_name(product))
    if minutes <= 0:
        await db.set_product_interval(pending.target_id, None)
        return _("🔄 Interval reset to the global one ({minutes} min)\n📦 {name}").format(
            minutes=_config(context).check_interval_minutes, name=name
        )
    if minutes < 5:
        raise _Retry(_("❌ Minimum is 5 minutes."))

    from price_tracker.bot.handlers.monitoring import _interval_caveat  # noqa: PLC0415 — cycle

    await db.set_product_interval(pending.target_id, minutes)
    return _("🔄 Check: every <b>{interval}</b>\n📦 {name}").format(
        interval=_format_minutes(minutes), name=name
    ) + _interval_caveat(minutes, _config(context).check_interval_minutes)


async def _do_admin_adduser(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Authorize a Telegram user by id."""
    try:
        new_uid = int(text.strip())
    except ValueError as exc:
        raise _Retry(_("❌ Invalid ID. It must be a number.")) from exc

    db = _db(context)
    existing = await db.get_user(new_uid)
    if existing and existing.get("is_active"):
        return _("ℹ️ User <code>{uid}</code> is already authorized.").format(uid=new_uid)

    await db.add_user(new_uid, is_admin=False)
    with contextlib.suppress(Exception):
        await context.bot.send_message(
            chat_id=new_uid, text=_("🎉 You have been authorized! Send /start.")
        )
    return _("✅ User <code>{uid}</code> added!").format(uid=new_uid)


async def _do_admin_nick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Rename a user. `target_id` is a *user* id here, never a product id."""
    nickname = text.strip()
    if not nickname:
        raise _Retry(_("❌ Empty nickname."))

    await _db(context).update_user_info(pending.target_id, display_name=nickname)
    return _("✅ Nickname updated: <b>{name}</b>").format(name=_escape_html(nickname))


async def _do_admin_interval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set the global check interval, with the same bounds as /setinterval."""
    try:
        minutes = int(text.strip())
    except ValueError as exc:
        raise _Retry(_("❌ Invalid number.")) from exc
    if minutes < 5:
        raise _Retry(_("❌ Minimum is 5 minutes."))
    if minutes > 1440 * 7:
        raise _Retry(_("❌ The maximum interval is 7 days."))

    await _db(context).set_config("check_interval_minutes", str(minutes))
    _reschedule_periodic_check(context, minutes)
    return _("✅ Interval updated: <b>every {interval}</b>").format(
        interval=_format_minutes(minutes)
    )


async def _do_admin_debug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Run the scraper debug report. This is the prompt that legitimately wants a URL."""
    url_input = text.strip()
    if not url_input.startswith("http"):
        raise _Retry(_("❌ Invalid URL."))

    from price_tracker.bot.handlers.debug import cmd_debug  # noqa: PLC0415

    context.args = [url_input]
    await cmd_debug(update, context)
    return _("🔧 Scraper debug: {url}").format(url=_escape_html(url_input[:80]))


async def _do_quiet_hours(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set or clear the window in which alerts are held back."""
    from price_tracker.bot.handlers.settings import _valid_hhmm  # noqa: PLC0415

    answer = text.strip().lower()
    repo = _db(context)
    user_id = update.effective_user.id
    if answer in ("off", "no", "none"):
        await update_prefs(repo, user_id, quiet_hours_start=None, quiet_hours_end=None)
        return _("🌙 Quiet hours disabled.")

    start, _sep, end = answer.partition("-")
    if not _sep or not _valid_hhmm(start.strip()) or not _valid_hhmm(end.strip()):
        raise _Retry(_("❌ Use 24h times, e.g. <code>22:00-08:00</code>."))
    start, end = start.strip(), end.strip()
    if start == end:
        raise _Retry(_("❌ Start and end cannot be the same time."))

    await update_prefs(repo, user_id, quiet_hours_start=start, quiet_hours_end=end)
    return _("🌙 Quiet hours: {start}–{end}.").format(start=start, end=end)


async def _do_timezone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set the timezone quiet hours are measured in."""
    from price_tracker.bot.handlers.settings import _VALID_TIMEZONES  # noqa: PLC0415

    tz = text.strip()
    if tz not in _VALID_TIMEZONES:
        raise _Retry(_("❌ Unknown timezone: {tz}").format(tz=_escape_html(tz[:40])))

    await update_prefs(_db(context), update.effective_user.id, timezone=tz)
    return _("🌍 Timezone set to {tz}.").format(tz=tz)


async def _do_throttle(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Cap how many alerts an hour get through, or remove the cap."""
    from price_tracker.bot.handlers.settings import describe_throttle  # noqa: PLC0415

    answer = text.strip().lower()
    limit: int | None = None
    if answer not in ("off", "no", "none", "0"):
        try:
            limit = int(answer)
        except ValueError as exc:
            raise _Retry(_("❌ Type a number, or <code>off</code> for no cap.")) from exc
        if limit <= 0:
            raise _Retry(_("❌ The limit must be greater than zero."))

    await update_prefs(_db(context), update.effective_user.id, throttle_per_hour=limit)
    return describe_throttle(limit)


async def _do_digest_interval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Set how long alerts are collected before a digest goes out."""
    from price_tracker.bot.handlers.settings import describe_digest  # noqa: PLC0415

    try:
        minutes = int(text.strip())
    except ValueError as exc:
        raise _Retry(_("❌ Invalid number.")) from exc
    if minutes <= 0:
        raise _Retry(_("❌ The interval must be greater than zero."))

    await update_prefs(
        _db(context),
        update.effective_user.id,
        digest_mode=True,
        digest_interval_minutes=minutes,
    )
    return describe_digest(enabled=True, interval=minutes)


async def _do_group_new(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str | None:
    """Create a group under the typed name, then ask what goes in it."""
    name = text.strip()[:60]
    if not name:
        raise _Retry(_("❌ The name cannot be empty."))

    db = _db(context)
    user_id = update.effective_user.id
    group_id = await db.create_group(user_id=user_id, name=name)
    if group_id is None:
        raise _Retry(
            _("❌ You already have a group called <b>{name}</b>.").format(name=_escape_html(name))
        )

    # An empty group is not the end of the task, it is the middle of it: go
    # straight to picking what goes in, rather than sending the user back to
    # /groups to find the group they just made.
    group = await db.get_group(group_id, user_id=user_id)
    if group is None or pending.prompt_message_id is None or pending.chat_id is None:
        return _("🏷 Group <b>{name}</b> created. Open /groups to fill it.").format(
            name=_escape_html(name)
        )

    from price_tracker.bot.handlers.callbacks._groups import (  # noqa: PLC0415 — import cycle
        open_add_picker,
    )
    from price_tracker.bot.handlers.callbacks._nav import EditById  # noqa: PLC0415 — cycle

    renderer = EditById(context.bot, pending.chat_id, pending.prompt_message_id)
    try:
        await open_add_picker(renderer, context, db, user_id, group)
    except TelegramError as exc:
        logger.debug("Could not open the picker on the prompt: %s", exc)
        return _("🏷 Group <b>{name}</b> created. Open /groups to fill it.").format(
            name=_escape_html(name)
        )
    return None


async def _do_group_rename(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> str:
    """Rename a group. `target_id` is a *group* id here."""
    name = text.strip()[:60]
    if not name:
        raise _Retry(_("❌ The name cannot be empty."))

    renamed = await _db(context).rename_group(
        pending.target_id, user_id=update.effective_user.id, name=name
    )
    if not renamed:
        raise _Retry(
            _("❌ You already have a group called <b>{name}</b>.").format(name=_escape_html(name))
        )
    return _("🏷 Renamed to <b>{name}</b>.").format(name=_escape_html(name))


_ACTIONS = {
    "target": _do_target,
    "threshold": _do_threshold,
    "refresh": _do_refresh,
    "admin_adduser": _do_admin_adduser,
    "admin_nick": _do_admin_nick,
    "admin_interval": _do_admin_interval,
    "admin_debug": _do_admin_debug,
    "quiet_hours": _do_quiet_hours,
    "timezone": _do_timezone,
    "throttle": _do_throttle,
    "digest_interval": _do_digest_interval,
    "group_new": _do_group_new,
    "group_rename": _do_group_rename,
}


@with_locale
@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cancel` — abandon whatever the bot is waiting for.

    There was no way to say this before except guessing one of a fixed, untranslated
    list of words ("no", "skip", "salta", "annulla", "cancel") that the bot never
    mentioned anywhere.
    """
    pending = clear_pending(context)
    if pending is None:
        await update.message.reply_text(_("👍 Nothing to cancel."))
        return
    await _show(update, context, pending, _("👍 Cancelled — nothing was changed."))


def register(app: Application) -> None:
    """Register the plain-text intake handler on `app`.

    One handler, not one per input shape: python-telegram-bot runs only the first
    match in a group, so two handlers would reintroduce the ordering bug this
    module exists to fix.
    """
    for name in ("cancel", "cancelar", "annulla"):
        app.add_handler(CommandHandler(name, cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
