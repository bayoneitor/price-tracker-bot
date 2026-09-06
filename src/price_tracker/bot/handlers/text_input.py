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
"""

from __future__ import annotations

import contextlib
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
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
from price_tracker.bot.handlers.settings import _reschedule_periodic_check
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
        await update.message.reply_text(_("👍 OK, nothing changed."))
        return

    spec = pending.spec
    if not spec.accepts_url and URL_PATTERN.search(text):
        # The prompt stays open: the user almost certainly meant to answer it, and
        # silently tracking the link is what made `/debug` unreachable before.
        await update.message.reply_text(
            _(
                "⏳ I am still waiting for {what}.\n"
                "Send /cancel first if you wanted to track that link instead."
            ).format(what=_(spec.label))
        )
        return

    user_id = update.effective_user.id
    product: dict[str, Any] | None = None
    if spec.needs_product:
        product = await _get_user_product(context, pending.target_id, user_id)
        if not product:
            clear_pending(context)
            await update.message.reply_text(_("❌ Product not found."))
            return
    elif pending.action.startswith("admin_") and not await _db(context).is_user_admin(user_id):
        clear_pending(context)
        await update.message.reply_text(_("⛔ Admin-only command."))
        return

    try:
        await _ACTIONS[pending.action](update, context, pending, text, product)
    except _Retry as retry:
        # Leave the prompt armed so the next message is read as another attempt.
        await update.message.reply_text(str(retry))
        return
    clear_pending(context)


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
# Each receives the resolved product when its spec asks for one, and raises
# `_Retry` when the answer cannot be used. Returning normally closes the prompt.


def _product_name(product: dict[str, Any] | None) -> str:
    return ((product or {}).get("name") or _("Unknown"))[:60]


async def _do_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
    """Set or clear a product's target price."""
    try:
        target = Decimal(text.replace(",", ".").replace("€", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise _Retry(_("❌ Invalid price. Try again.")) from exc

    db = _db(context)
    product_id = pending.target_id
    if target <= 0:
        await db.set_target_price(product_id, None)
        await update.message.reply_text(_("🎯 Target cleared for #{pid}.").format(pid=product_id))
        return

    await db.set_target_price(product_id, target)
    assert product is not None
    current = _safe_dec(product.get("current_price"))
    currency = product.get("currency", "EUR")
    msg = _("🎯 Target: <b>{target}</b>\n📦 {name}").format(
        target=_convert_display(target, currency), name=_escape_html(_product_name(product))
    )
    if current and target < current:
        diff_pct = ((current - target) / current) * 100
        msg += _("\n💰 Current: {price} (-{pct:.1f}% needed)").format(
            price=_convert_display(current, currency), pct=diff_pct
        )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def _do_threshold(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
    """Set a product's price-drop threshold."""
    try:
        threshold_type, threshold_value = _parse_threshold_input(text)
    except ValueError as exc:
        raise _Retry(_("❌ Invalid value. Try again (e.g. 20% or 50).")) from exc

    await _db(context).set_threshold(pending.target_id, threshold_type, threshold_value)
    await update.message.reply_text(
        _("🎯 Threshold: <b>{threshold}</b>\n📦 {name}").format(
            threshold=_format_threshold(threshold_type, threshold_value),
            name=_escape_html(_product_name(product)),
        ),
        parse_mode=ParseMode.HTML,
    )


async def _do_refresh(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
    """Set a product's own check interval, or hand it back to the global one."""
    try:
        minutes = int(text.strip())
    except ValueError as exc:
        raise _Retry(_("❌ Invalid number. Try again.")) from exc

    db = _db(context)
    name = _escape_html(_product_name(product))
    if minutes <= 0:
        await db.set_product_interval(pending.target_id, None)
        await update.message.reply_text(
            _("🔄 Interval reset to the global one ({minutes} min)\n📦 {name}").format(
                minutes=_config(context).check_interval_minutes, name=name
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    if minutes < 5:
        raise _Retry(_("❌ Minimum is 5 minutes."))

    await db.set_product_interval(pending.target_id, minutes)
    await update.message.reply_text(
        _("🔄 Check: every <b>{interval}</b>\n📦 {name}").format(
            interval=_format_minutes(minutes), name=name
        ),
        parse_mode=ParseMode.HTML,
    )


async def _do_admin_adduser(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
    """Authorize a Telegram user by id."""
    try:
        new_uid = int(text.strip())
    except ValueError as exc:
        raise _Retry(_("❌ Invalid ID. It must be a number.")) from exc

    db = _db(context)
    existing = await db.get_user(new_uid)
    if existing and existing.get("is_active"):
        await update.message.reply_text(
            _("ℹ️ User <code>{uid}</code> is already authorized.").format(uid=new_uid),
            parse_mode=ParseMode.HTML,
        )
        return

    await db.add_user(new_uid, is_admin=False)
    await update.message.reply_text(
        _("✅ User <code>{uid}</code> added!").format(uid=new_uid),
        parse_mode=ParseMode.HTML,
    )
    with contextlib.suppress(Exception):
        await context.bot.send_message(
            chat_id=new_uid, text=_("🎉 You have been authorized! Send /start.")
        )


async def _do_admin_nick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
    """Rename a user. `target_id` is a *user* id here, never a product id."""
    nickname = text.strip()
    if not nickname:
        raise _Retry(_("❌ Empty nickname."))

    await _db(context).update_user_info(pending.target_id, display_name=nickname)
    await update.message.reply_text(
        _("✅ Nickname updated: <b>{name}</b>").format(name=_escape_html(nickname)),
        parse_mode=ParseMode.HTML,
    )


async def _do_admin_interval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
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
    await update.message.reply_text(
        _("✅ Interval updated: <b>every {interval}</b>").format(interval=_format_minutes(minutes)),
        parse_mode=ParseMode.HTML,
    )


async def _do_admin_debug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingInput,
    text: str,
    product: dict[str, Any] | None,
) -> None:
    """Run the scraper debug report. This is the prompt that legitimately wants a URL."""
    url_input = text.strip()
    if not url_input.startswith("http"):
        raise _Retry(_("❌ Invalid URL."))

    from price_tracker.bot.handlers.debug import cmd_debug  # noqa: PLC0415

    context.args = [url_input]
    await cmd_debug(update, context)


_ACTIONS = {
    "target": _do_target,
    "threshold": _do_threshold,
    "refresh": _do_refresh,
    "admin_adduser": _do_admin_adduser,
    "admin_nick": _do_admin_nick,
    "admin_interval": _do_admin_interval,
    "admin_debug": _do_admin_debug,
}


def register(app: Application) -> None:
    """Register the plain-text intake handler on `app`.

    One handler, not one per input shape: python-telegram-bot runs only the first
    match in a group, so two handlers would reintroduce the ordering bug this
    module exists to fix.
    """
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
