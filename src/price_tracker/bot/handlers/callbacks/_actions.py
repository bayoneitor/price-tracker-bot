"""Per-product action callbacks (`edit_*`, `pause_*`, `remove_*`, `reset_*`,
`reactivate_*`, `set*_*` pickers).

Split out of `handlers/callbacks/_product.py` to keep each module under the
500-LOC budget [Task 17].
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton
from telegram.constants import ParseMode

from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _format_threshold,
    _safe_dec,
    resolve_owned_product,
)
from price_tracker.bot.keyboards import prompt_keyboard, result_keyboard
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import set_pending

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _message_id(query: Any) -> int | None:
    """The id of the message a callback arrived on, if it still has one."""
    return getattr(getattr(query, "message", None), "message_id", None)


async def handle_edit_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Edit' button (`edit_<id>`)."""
    if not data.startswith("edit_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "edit_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    name = (product.get("name") or _("Unknown"))[:60]
    threshold_type = product.get("threshold_type", "percentage")
    threshold_value = product.get("threshold_value", "10")
    threshold_str = _format_threshold(threshold_type, threshold_value)
    target = _safe_dec(product.get("target_price"))
    target_str = f"€{target:.2f}" if target else _("not set")

    initial = _safe_dec(product.get("initial_price"))
    current = _safe_dec(product.get("current_price"))
    initial_str = f"€{initial:.2f}" if initial else _("N/A")

    edit_buttons = [
        [InlineKeyboardButton(_("🔔 Every drop"), callback_data=f"track_any_{product_id}")],
        [
            InlineKeyboardButton(
                _("📉 Threshold % or €"), callback_data=f"track_threshold_{product_id}"
            )
        ],
        [InlineKeyboardButton(_("💰 Target price"), callback_data=f"track_target_{product_id}")],
    ]
    if initial and current and initial != current:
        edit_buttons.append(
            [InlineKeyboardButton(_("🔄 Reset base price"), callback_data=f"reset_{product_id}")]
        )

    # Edits the caller's message rather than adding one: opened from the listing
    # this panel used to leave the listing sitting above it, and every product the
    # user peeked at left another panel behind.
    await query.edit_message_text(
        _(
            "✏️ <b>Edit #{pid}</b> {name}\n\n"
            "🎯 Current threshold: <b>{threshold}</b>\n"
            "🏁 Current target: <b>{target}</b>\n"
            "📌 Base price: <b>{initial}</b>\n\n"
            "<b>What do you want to change?</b>"
        ).format(
            pid=product_id,
            name=_escape_html(name),
            threshold=threshold_str,
            target=target_str,
            initial=initial_str,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query), *edit_buttons),
    )
    return True


async def handle_pause_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Pause' button (`pause_<id>`)."""
    if not data.startswith("pause_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "pause_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    name = (product.get("name") or _("Unknown"))[:50]
    await db.deactivate_product(product_id)
    await query.edit_message_text(
        _("⏸ <b>Paused:</b> {name}\nUse /reactivate {pid} to resume it.").format(
            name=_escape_html(name), pid=product_id
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query)),
    )
    return True


async def handle_remove_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Delete' button (`remove_<id>`) — shows confirmation prompt."""
    if not data.startswith("remove_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "remove_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved

    name = (product.get("name") or _("Unknown"))[:50]
    choices = [
        InlineKeyboardButton(
            _("🗑 Yes, delete everything"),
            callback_data=f"confirm_delete_{product_id}",
        ),
        InlineKeyboardButton(_("⏸ Just pause"), callback_data=f"pause_{product_id}"),
    ]
    await query.edit_message_text(
        _("❓ What do you want to do with <b>{name}</b>?").format(name=_escape_html(name)),
        parse_mode=ParseMode.HTML,
        # The old "❌ Cancel" here dropped the user on a dead "Operation cancelled"
        # screen; ◀️ Back puts them where they were.
        reply_markup=result_keyboard(context, _message_id(query), choices),
    )
    return True


async def handle_reset_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Reset base price' button (`reset_<id>`)."""
    if not data.startswith("reset_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "reset_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved
    success = await db.reset_initial_price(product_id)
    if success:
        name = (product.get("name") or _("Unknown"))[:60]
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else _("N/A")
        await query.edit_message_text(
            _(
                "✅ Base price updated!\n\n📦 <b>#{pid}</b> {name}\n💰 New base: <b>{price}</b>"
            ).format(pid=product_id, name=_escape_html(name), price=price_str),
            parse_mode=ParseMode.HTML,
            reply_markup=result_keyboard(context, _message_id(query)),
        )
    else:
        await query.edit_message_text(
            _("❌ Update failed."),
            reply_markup=result_keyboard(context, _message_id(query)),
        )
    return True


async def handle_reactivate_button(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the 'Reactivate' button (`reactivate_<id>`)."""
    if not data.startswith("reactivate_"):
        return False

    resolved = await resolve_owned_product(query, context, data, "reactivate_", user_id)
    if resolved is None:
        return True
    product_id, product = resolved
    await db.reactivate_product(product_id)
    name = (product.get("name") or _("Unknown"))[:50]
    await query.edit_message_text(
        _("▶️ <b>Reactivated:</b> {name}").format(name=_escape_html(name)),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query)),
    )
    return True


def _target_prompt(product: dict[str, Any]) -> str:
    name = _escape_html((product.get("name") or _("Unknown"))[:50])
    current = _safe_dec(product.get("current_price"))
    price_info = _(" (current: €{price:.2f})").format(price=current) if current else ""
    return _(
        "🎯 <b>{name}</b>{price_info}\n\nType the target price (e.g. <code>29.99</code>):"
    ).format(name=name, price_info=price_info)


def _threshold_prompt(product: dict[str, Any]) -> str:
    return _(
        "🎯 <b>{name}</b>\n\nType the threshold (e.g. <code>20%</code> or <code>50</code>):"
    ).format(name=_escape_html((product.get("name") or _("Unknown"))[:50]))


def _refresh_prompt(product: dict[str, Any]) -> str:
    return _(
        "🔄 <b>{name}</b>\n\n"
        "Type the interval in minutes (e.g. <code>30</code>, <code>720</code> for 12h):"
    ).format(name=_escape_html((product.get("name") or _("Unknown"))[:50]))


# Built per call, never at import: a module-level table would freeze whichever
# locale happened to be active when this module was first imported.
_PROMPTS = {
    "target": _target_prompt,
    "threshold": _threshold_prompt,
    "refresh": _refresh_prompt,
}


async def handle_picker(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle inline pickers that need a follow-up text reply (`set*_<id>`)."""
    for prefix, action in (
        ("settarget_", "target"),
        ("setsoglia_", "threshold"),
        ("setrefresh_", "refresh"),
    ):
        if not data.startswith(prefix):
            continue
        resolved = await resolve_owned_product(query, context, data, prefix, user_id)
        if resolved is None:
            return True
        product_id, product = resolved
        set_pending(context, action, product_id, message=query.message)
        await query.edit_message_text(
            _PROMPTS[action](product),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    return False
