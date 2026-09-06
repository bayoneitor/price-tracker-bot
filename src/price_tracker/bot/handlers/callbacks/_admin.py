"""Admin-only callback handlers (`menu_admin*`, `admin_rm_*`, `admin_nick_*`).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget [Task 17].
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

from price_tracker.bot.decorators import _config
from price_tracker.bot.handlers._helpers import _escape_html, _parse_id
from price_tracker.bot.keyboards import close_button, menu_exit_row, prompt_keyboard
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import set_pending

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _message_id(query: Any) -> int | None:
    """The id of the message a callback arrived on, if it still has one."""
    return getattr(getattr(query, "message", None), "message_id", None)


def _settings_exit_row() -> list[InlineKeyboardButton]:
    """Up to the admin settings screen, or out of the panel entirely.

    Rebuilt per call: a module-level constant would freeze whichever locale
    happened to be active at import time.
    """
    return [
        InlineKeyboardButton(_("◀️ Settings"), callback_data="menu_admin"),
        close_button(),
    ]


def _back_to_admin() -> InlineKeyboardMarkup:
    """The exit row on its own, for screens with no other buttons."""
    return InlineKeyboardMarkup([_settings_exit_row()])


async def handle_admin_menu(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the admin menu callbacks. Returns True if data was handled."""
    if data == "menu_admin":
        if not await db.is_user_admin(user_id):
            return True  # silent reject — handled
        users = await db.get_all_users()
        config = _config(context)
        saved = await db.get_config("check_interval_minutes")
        interval = int(saved) if saved else config.check_interval_minutes
        rows = [
            [InlineKeyboardButton(_("👥 User list"), callback_data="menu_admin_users")],
            [
                InlineKeyboardButton(_("➕ Add user"), callback_data="menu_admin_adduser"),
                InlineKeyboardButton(_("🚫 Remove user"), callback_data="menu_admin_removeuser"),
            ],
            [InlineKeyboardButton(_("✏️ User nickname"), callback_data="menu_admin_nick")],
            [
                InlineKeyboardButton(
                    _("⏱ Global interval: {minutes} min").format(minutes=interval),
                    callback_data="menu_admin_interval",
                )
            ],
            [InlineKeyboardButton(_("🔧 Scraper debug"), callback_data="menu_admin_debug")],
            menu_exit_row(),
        ]
        await query.edit_message_text(
            _(
                "👑 <b>Settings</b>\n\n👥 Active users: {users}\n⏱ Global interval: {minutes} min"
            ).format(users=len(users), minutes=interval),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_admin_users":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.get_all_users()
        txt = [_("👥 <b>Users</b>\n")]
        for u in users:
            uid = u["user_id"]
            nm = u.get("display_name") or u.get("username") or _("N/A")
            role = "👑" if u.get("is_admin") else "👤"
            st = await db.get_stats(uid)
            txt.append(
                f"{role} <code>{uid}</code> {_escape_html(str(nm))} — {st['active_products']} prod."
            )
        await query.edit_message_text(
            chr(10).join(txt),
            parse_mode=ParseMode.HTML,
            reply_markup=_back_to_admin(),
        )
        return True

    if data == "menu_admin_adduser":
        if not await db.is_user_admin(user_id):
            return True
        set_pending(context, "admin_adduser", message=query.message)
        await query.edit_message_text(
            _("➕ <b>Add user</b>\n\nType the Telegram ID of the user to add:"),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    if data == "menu_admin_removeuser":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.get_all_users()
        removable = [u for u in users if not u.get("is_admin") and u["user_id"] != user_id]
        if not removable:
            await query.edit_message_text(
                _("❌ No removable users."), reply_markup=_back_to_admin()
            )
            return True
        rows = []
        for u in removable:
            nm = u.get("display_name") or u.get("username") or str(u["user_id"])
            rows.append(
                [InlineKeyboardButton(f"🚫 {nm}", callback_data=f"admin_rm_{u['user_id']}")]
            )
        rows.append(_settings_exit_row())
        await query.edit_message_text(
            _("🚫 <b>Remove user</b>\n\nTap to remove:"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("admin_rm_"):
        if not await db.is_user_admin(user_id):
            return True
        target_id = _parse_id(data.replace("admin_rm_", ""))
        if target_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        removed = await db.remove_user(target_id)
        if removed:
            await query.edit_message_text(
                _("✅ User <code>{uid}</code> removed.").format(uid=target_id),
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_admin(),
            )
        else:
            await query.edit_message_text(_("❌ User not found."), reply_markup=_back_to_admin())
        return True

    if data == "menu_admin_nick":
        if not await db.is_user_admin(user_id):
            return True
        users = await db.get_all_users()
        rows = []
        for u in users:
            nm = u.get("display_name") or u.get("username") or str(u["user_id"])
            rows.append(
                [InlineKeyboardButton(f"✏️ {nm}", callback_data=f"admin_nick_{u['user_id']}")]
            )
        rows.append(_settings_exit_row())
        await query.edit_message_text(
            _("✏️ <b>Nickname</b>\n\nPick a user:"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("admin_nick_"):
        if not await db.is_user_admin(user_id):
            return True
        target_id = _parse_id(data.replace("admin_nick_", ""))
        if target_id is None:
            await query.edit_message_text(_("❌ Invalid ID."))
            return True
        set_pending(context, "admin_nick", target_id, message=query.message)
        u = await db.get_user(target_id)
        current_name = u.get("display_name", _("N/A")) if u else _("N/A")
        await query.edit_message_text(
            _("✏️ <b>Nickname for {uid}</b>\nCurrent: {name}\n\nType the new nickname:").format(
                uid=target_id, name=_escape_html(str(current_name))
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    if data == "menu_admin_interval":
        if not await db.is_user_admin(user_id):
            return True
        set_pending(context, "admin_interval", message=query.message)
        await query.edit_message_text(
            _(
                "⏱ <b>Global interval</b>\n\n"
                "Type the minutes (e.g. <code>60</code>, <code>360</code>):"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    if data == "menu_admin_debug":
        if not await db.is_user_admin(user_id):
            return True
        set_pending(context, "admin_debug", message=query.message)
        await query.edit_message_text(
            _("🔧 <b>Scraper debug</b>\n\nPaste the URL of the product to analyse:"),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    return False
