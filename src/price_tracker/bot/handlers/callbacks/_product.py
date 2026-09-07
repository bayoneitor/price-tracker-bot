"""Product-scoped callback handlers (delete/check/chart/edit/pause/remove/...).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget [Task 17]. Each function takes the `(query, context, db,
user_id, data)` tuple and returns `True` if it handled the callback, `False`
otherwise — keeps the dispatcher a thin if/elif on prefixes.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

from price_tracker.bot.charts import generate_chart
from price_tracker.bot.decorators import (
    _convert_display,
)
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    _safe_dec,
    resolve_owned_product,
)
from price_tracker.bot.keyboards import (
    build_threshold_keyboard,
    prompt_keyboard,
    result_keyboard,
)
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import forget_product, set_pending, transfer_nav

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _message_id(query: Any) -> int | None:
    """The id of the message a callback arrived on, if it still has one."""
    return getattr(getattr(query, "message", None), "message_id", None)


async def _back_to_index(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, notice: str
) -> None:
    """Report a deletion and land back on the listing, in the same message.

    Deleting used to end on a screen saying only what had happened, whose one way
    onward was ◀️ Back. But deleting is rarely the last thing you do — you are
    tidying a list — so the listing is where the reader already wanted to be, and
    it is also the proof: the product is not in it any more.
    """
    from price_tracker.bot.handlers.callbacks._list import _extra_rows  # noqa: PLC0415 — cycle
    from price_tracker.bot.handlers.product_list import (  # noqa: PLC0415 — cycle
        LIST_MESSAGE_KEY,
        build_index_view,
    )
    from price_tracker.bot.keyboards import LIST_GOTO_PREFIX  # noqa: PLC0415 — cycle
    from price_tracker.bot.navigation import push_nav  # noqa: PLC0415 — cycle

    message_id = _message_id(query)
    products = await db.get_active_products(user_id)
    # From the first page: the one the reader was on may not exist any more, and
    # after deleting the last product on a page nor would the page.
    text, keyboard = build_index_view(
        products,
        0,
        context=context,
        message_id=message_id,
        extra_rows=await _extra_rows(db, user_id),
    )
    await query.edit_message_text(
        f"{notice}\n\n{text}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    if context.user_data is not None and message_id is not None:
        # This message is the listing now: a typed number steers it, and the
        # trail has to say so or ◀️ Back would return to the product's own screen.
        context.user_data[LIST_MESSAGE_KEY] = message_id
        push_nav(context, message_id, f"{LIST_GOTO_PREFIX}0")


async def handle_delete_flow(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the delete confirmation flow (`confirm_delete_*`, `cancel_delete`,
    `delete_all`, `confirmdeleteall`).
    """
    if data.startswith("confirm_delete_"):
        product_id = _parse_id(data.replace("confirm_delete_", ""))
        if product_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        product = await _get_user_product(context, product_id, user_id)
        if product:
            name = product.get("name") or _("Unknown")
            await db.delete_product(product_id, user_id=user_id)
            # Before anything renders: the trail still holds the screens of the
            # product just deleted, and the index about to draw itself reads it.
            forget_product(context, product_id)
            await _back_to_index(
                query,
                context,
                db,
                user_id,
                _("🗑 Removed: <b>{name}</b> — its history is kept.").format(
                    name=_escape_html(name[:80])
                ),
            )
        else:
            await query.edit_message_text(
                _("❌ Product not found or not authorized."),
                reply_markup=result_keyboard(context, _message_id(query)),
            )
        return True

    if data == "cancel_delete":
        await query.edit_message_text(
            _("👍 Operation cancelled."),
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return True

    if data == "delete_all":
        products = await db.get_active_products(user_id)
        count = len(products)
        if count == 0:
            await query.edit_message_text(_("📭 No products to delete."))
            return True

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _("⚠️ Yes, delete all ({count})").format(count=count),
                        callback_data="confirmdeleteall",
                    ),
                    InlineKeyboardButton(_("❌ Cancel"), callback_data="cancel_delete"),
                ]
            ]
        )
        await query.edit_message_text(
            _(
                "🚨 <b>Warning!</b>\n\n"
                "You are about to <b>remove {count} products</b> from your list "
                "and stop tracking them.\n\n"
                "Their price history is kept, and each one comes back if you add "
                "its link again."
            ).format(count=count),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True

    if data == "confirmdeleteall":
        products = await db.get_active_products(user_id)
        count = 0
        for p in products:
            await db.delete_product(p["id"], user_id=user_id)
            forget_product(context, p["id"])
            count += 1
        await _back_to_index(
            query,
            context,
            db,
            user_id,
            _("🗑 <b>Removed {count} products.</b> Their history is kept.").format(count=count),
        )
        return True

    return False


async def handle_check_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Check now' button (`check_<id>`)."""
    if not data.startswith("check_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "check_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    await query.edit_message_text(_("⏳ Checking price..."))
    from price_tracker.core.scraper_base import detect_currency  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    try:
        result = await scheduler.check_one_product_for_user(product_id=product_id, user_id=user_id)
    except Exception as e:  # noqa: BLE001 — surface error to user
        await query.edit_message_text(_("❌ Error: {error}").format(error=e))
        return True
    alert = result.alert

    product = await db.get_product(product_id)
    if product is None:
        await query.edit_message_text(_("❌ Product not found."))
        return True
    current = _safe_dec(product.get("current_price"))
    initial = _safe_dec(product.get("initial_price"))
    p_currency = product.get("currency", "") or detect_currency(product.get("url", "")) or "EUR"
    price_str = _convert_display(current, p_currency) if current else _("N/A")

    text = _("✅ <b>#{pid}</b> {name}\n💰 Price: {price}").format(
        pid=product_id, name=_escape_html(product_label(product)), price=price_str
    )
    if initial and current and initial > 0 and initial != current:
        diff = (initial - current) / initial * 100
        if diff > 0:
            text += _("\n📌 Initial: €{initial:.2f} (<i>-{diff:.1f}% since tracking</i>)").format(
                initial=initial, diff=diff
            )

    if alert:
        text += _("\n\n🔔 <b>PRICE JUST DROPPED!</b>")
        text += _("\n💸 Was: €{old:.2f} → Now: €{new:.2f}").format(
            old=alert.old_price, new=alert.new_price
        )

    keyboard = result_keyboard(
        context,
        _message_id(query),
        [InlineKeyboardButton(_("📊 Price history"), callback_data=f"chart_{product_id}")],
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return True


async def handle_chart_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the per-product 'Price history' button (`chart_<id>`).

    The chart replaces the panel it was opened from rather than piling up under
    it. Telegram cannot edit a text message into a photo, so the panel is deleted
    and the photo sent — and the navigation trail moves with it, which is what
    lets ◀️ Back reopen the panel afterwards.
    """
    if not data.startswith("chart_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "chart_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    origin_id = _message_id(query)
    chart = await generate_chart(db, product_id, product)
    if not chart:
        await query.edit_message_text(
            _("📭 Not enough data to generate the chart (at least 2 points needed)."),
            reply_markup=result_keyboard(context, origin_id),
        )
        return True

    # The image itself carries only "#id · shop"; the caption has room for the name.
    caption = f"📊 <b>#{product_id}</b> {_escape_html(product_label(product))}"

    # Built against the panel's trail, which the photo is about to inherit.
    keyboard = result_keyboard(context, origin_id)
    with contextlib.suppress(TelegramError):
        await query.message.delete()
    photo = await query.message.reply_photo(
        photo=InputFile(chart, filename=f"chart_{product_id}.png"),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    if origin_id is not None:
        transfer_nav(context, origin_id, photo.message_id)
    return True


# msgid strings, translated lazily at call time so the module-level table does
# not freeze the locale that happened to be active at import.
_PREF_PROMPTS: dict[str, tuple[str | None, str | None, str]] = {
    "pref_new_": ("new", None, "🆕 Preference: <b>New only</b>"),
    "pref_used_": ("used", None, "♻️ Preference: <b>Used only</b>"),
    "pref_amazon_": (None, "amazon", "📦 Preference: <b>Sold by Amazon only</b>"),
    "pref_anyseller_": (None, "any", "🏪 Preference: <b>Any seller</b>"),
    "pref_default_": (None, None, "👍 Preference: <b>No filter</b>"),
}


async def handle_amazon_pref(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle Amazon condition/seller preference buttons (`pref_*`)."""
    for prefix, (condition, seller, label) in _PREF_PROMPTS.items():
        if data.startswith(prefix):
            resolved = await resolve_owned_product(query, context, data, prefix, user_id)
            if resolved is None:
                return True
            product_id, product = resolved
            await db.set_product_preferences(product_id, condition=condition, seller=seller)
            name = (product.get("name") or _("Unknown"))[:60]
            await query.edit_message_text(
                _("{label} for #{pid}\n📦 {name}\n\n<b>How do you want to be notified?</b>").format(
                    label=_(label), pid=product_id, name=_escape_html(name)
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=build_threshold_keyboard(product_id, context, _message_id(query)),
            )
            return True
    return False


async def handle_track_choice(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle tracking-mode choice buttons (`track_*`)."""
    if data.startswith("track_any_"):
        resolved = await resolve_owned_product(query, context, data, "track_any_", user_id)
        if resolved is None:
            return True
        product_id, product = resolved
        await db.set_threshold(product_id, "any_drop", "0")
        name = (product.get("name") or _("Unknown"))[:60]
        await query.edit_message_text(
            _(
                "🔔 <b>Every drop</b> enabled for #{pid}\n"
                "📦 {name}\n\n"
                "You will get a notification on every price drop."
            ).format(pid=product_id, name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return True

    if data.startswith("track_threshold_"):
        resolved = await resolve_owned_product(query, context, data, "track_threshold_", user_id)
        if resolved is None:
            return True
        product_id, product = resolved
        name = (product.get("name") or _("Unknown"))[:60]
        set_pending(context, "threshold", product_id, message=query.message)
        await query.edit_message_text(
            _(
                "📉 <b>Set threshold for #{pid}</b>\n"
                "📦 {name}\n\n"
                "Type the threshold you want:\n"
                "• <code>20%</code> — alert me if it drops by 20%\n"
                "• <code>50</code> — alert me if it drops by €50"
            ).format(pid=product_id, name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    if data.startswith("track_target_"):
        resolved = await resolve_owned_product(query, context, data, "track_target_", user_id)
        if resolved is None:
            return True
        product_id, product = resolved
        name = (product.get("name") or _("Unknown"))[:60]
        current = _safe_dec(product.get("current_price"))
        currency = product.get("currency", "EUR")
        price_hint = (
            _("\n💰 Current price: {price}").format(price=_convert_display(current, currency))
            if current
            else ""
        )
        set_pending(context, "target", product_id, message=query.message)
        await query.edit_message_text(
            _(
                "💰 <b>Set target price for #{pid}</b>\n"
                "📦 {name}{hint}\n\n"
                "Type the price you are aiming for (e.g. <code>100</code>):"
            ).format(pid=product_id, name=_escape_html(name), hint=price_hint),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    if data.startswith("track_default_"):
        resolved = await resolve_owned_product(query, context, data, "track_default_", user_id)
        if resolved is None:
            return True
        product_id, product = resolved
        await db.set_threshold(product_id, "percentage", "10")
        name = (product.get("name") or _("Unknown"))[:60]
        await query.edit_message_text(
            _(
                "👍 <b>Default threshold -10%</b> for #{pid}\n"
                "📦 {name}\n\n"
                "You will get a notification when the price drops 10% "
                "from the initial price."
            ).format(pid=product_id, name=_escape_html(name)),
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return True

    return False


# Per-product action callbacks (edit/pause/remove/reset/reactivate/pickers)
# live in `_actions.py` to keep this module under the 500-LOC budget.
