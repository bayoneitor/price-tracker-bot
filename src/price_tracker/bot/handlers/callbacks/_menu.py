"""Main-menu callback handlers (non-admin).

Split out of `handlers/callbacks/__init__.py` to keep the dispatcher under
the 500-LOC budget [Task 17].
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode

from price_tracker.bot.decorators import _config
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _safe_dec,
)
from price_tracker.bot.keyboards import (
    LIST_GOTO_PREFIX,
    build_main_menu,
    menu_exit_row,
)
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Screens the menu no longer has, and where their buttons now land. Panels sent
# before the menu was reordered are still in people's chats, and their buttons
# still send the old token; the dispatcher rewrites it before anything sees it.
RETIRED_SCREENS = {
    "cmd_lista": f"{LIST_GOTO_PREFIX}0",
    "menu_prodotti": f"{LIST_GOTO_PREFIX}0",
    "menu_prezzi": f"{LIST_GOTO_PREFIX}0",
    "menu_storia": f"{LIST_GOTO_PREFIX}0",
    "menu_notifiche": "menu_delivery",
}

# Export column headers are a data contract, not UI: they stay untranslated so
# a CSV exported under one locale still imports under another. `cmd_import`
# also accepts the legacy Italian headers (see product_io.CSV_ALIASES).
CSV_HEADERS = [
    "ID",
    "Name",
    "URL",
    "Initial Price",
    "Current Price",
    "Lowest Price",
    "Target",
    "Threshold",
    "Active",
    "Currency",
]


async def handle_menu_navigation(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Handle the non-admin menu callbacks (`menu_*`, `cmd_lista`).

    Returns `True` if the callback was a known menu action; `False` lets
    the caller try the next handler.
    """
    if data == "menu_main":
        text, keyboard = build_main_menu(await db.is_user_admin(query.from_user.id))
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return True

    if data == "menu_add":
        await query.edit_message_text(
            _(
                "➕ <b>Track a new product</b>\n\n"
                "Paste the product's link into the chat — that is all it takes.\n"
                "You can also send <code>/add &lt;link&gt;</code>."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
        )
        return True

    if data == "menu_paused":
        all_prods = await db.get_all_products(user_id)
        paused = [p for p in all_prods if not p.get("is_active")]
        rows = []
        for p in paused[:10]:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"▶️ #{p['id']} {product_label(p)}",
                        callback_data=f"reactivate_{p['id']}",
                    )
                ]
            )
        rows.append(menu_exit_row())
        await query.edit_message_text(
            _("⏸ <b>Paused products</b> ({count})\n\nTap one to reactivate it.").format(
                count=len(paused)
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_checkall":
        return await _handle_menu_checkall(query, context, db, user_id)

    if data == "menu_dati":
        rows = [
            [InlineKeyboardButton(_("💾 Export CSV"), callback_data="menu_esporta")],
            [
                InlineKeyboardButton(
                    _("📂 Import CSV — send the file in chat"),
                    callback_data="menu_importa_info",
                )
            ],
            menu_exit_row(),
        ]
        await query.edit_message_text(
            # The counters live in Status; repeating them here was the same three
            # numbers on two screens.
            _(
                "💾 <b>Import / Export</b>\n\n"
                "Export gives you a CSV of everything you track; import reads one back."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data == "menu_esporta":
        return await _handle_menu_esporta(query, context, db, user_id)

    if data == "menu_importa_info":
        await query.edit_message_text(
            _(
                "📂 <b>Import products</b>\n\n"
                "Send a CSV file in chat (the one produced by Export).\n"
                "Duplicates will be skipped."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
        )
        return True

    if data == "menu_info":
        return await _handle_menu_info(query, context, db, user_id)

    if data == "menu_commands":
        return await _handle_menu_commands(query)

    if data == "menu_errors":
        return await _handle_menu_errors(query, context, db, user_id)

    return False


async def _handle_menu_checkall(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> bool:
    """Run /checkall via the menu button."""
    products = await db.get_active_products(user_id)
    if not products:
        await query.edit_message_text(
            _("📭 No products."),
            reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
        )
        return True
    await query.edit_message_text(_("🔍 Checking {count} products...").format(count=len(products)))
    from price_tracker.core.alert import format_alert  # noqa: PLC0415

    scheduler = context.bot_data["scheduler"]
    # Interactive caller: small per-product pause (see cmd_checkall in monitoring.py).
    results = await scheduler.check_user_products_for_user(
        user_id=user_id, delay_between_products=0.5
    )
    alerts = [r.alert for r in results if r.alert is not None]
    updated = await db.get_active_products(user_id)
    txt_lines = [_("✅ <b>Done</b> — {count} products").format(count=len(updated)) + chr(10)]
    for p in updated:
        nm = product_label(p)
        cur = _safe_dec(p.get("current_price"))
        ini = _safe_dec(p.get("initial_price"))
        tag = f"€{cur:.2f}" if cur else _("N/A")
        diff = ""
        if ini and cur and ini > 0 and ini != cur:
            d = (ini - cur) / ini * 100
            diff = f" <i>(-{d:.1f}%)</i>" if d > 0 else f" <i>(+{abs(d):.1f}%)</i>"
        txt_lines.append(f"  #{p['id']} {_escape_html(nm)} — {tag}{diff}")
    if alerts:
        txt_lines.append(chr(10) + _("🔔 <b>{count} changes!</b>").format(count=len(alerts)))
    await query.edit_message_text(
        chr(10).join(txt_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
    )
    for a in alerts:
        await query.message.reply_text(
            format_alert(a), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    return True


async def _handle_menu_esporta(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> bool:
    """Export CSV via the menu.

    The file has to be a new message, but the panel it was requested from is
    rewritten rather than left sitting there as a menu that already ran.
    """
    products = await db.get_all_products(user_id)
    if not products:
        await query.edit_message_text(
            _("📭 No products."),
            reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
        )
        return True
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADERS)
    for p in products:
        w.writerow(
            [
                p["id"],
                p.get("name", ""),
                p.get("url", ""),
                p.get("initial_price", ""),
                p.get("current_price", ""),
                p.get("lowest_price", ""),
                p.get("target_price", ""),
                f"{p.get('threshold_type', 'percentage')}:{p.get('threshold_value', '10')}",
                "Yes" if p.get("is_active") else "No",
                p.get("currency", "EUR"),
            ]
        )
    await query.message.reply_document(
        document=InputFile(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            filename=f"products_{datetime.now().strftime('%Y%m%d')}.csv",
        ),
        caption=_("💾 {count} products exported.").format(count=len(products)),
    )
    await query.edit_message_text(
        _("💾 <b>Export</b>\n\n{count} products sent as a CSV file below.").format(
            count=len(products)
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
    )
    return True


async def _handle_menu_info(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> bool:
    """Render the user-facing stats panel."""
    config = _config(context)
    stats = await db.get_stats(user_id)
    is_admin = await db.is_user_admin(user_id)
    saved = await db.get_config("check_interval_minutes")
    interval = int(saved) if saved else config.check_interval_minutes
    int_str = f"{interval // 60}h" if interval >= 60 and interval % 60 == 0 else f"{interval}min"
    text = _(
        "📊 <b>Stats</b>\n\n"
        "📦 Active products: {active}\n"
        "📁 Total: {total}\n"
        "🔍 Checks: {checks}\n"
        "⏱ Interval: every {interval}"
    ).format(
        active=stats["active_products"],
        total=stats["total_products"],
        checks=stats["total_checks"],
        interval=int_str,
    )
    if is_admin:
        gs = await db.get_stats()
        users = await db.get_all_users()
        text += _(
            "\n\n👑 <b>Admin</b>\n"
            "👥 Users: {users}\n"
            "📦 Global products: {products}\n"
            "🔍 Global checks: {checks}"
        ).format(users=len(users), products=gs["active_products"], checks=gs["total_checks"])
    rows = [
        [
            InlineKeyboardButton(_("⚠️ Errors"), callback_data="menu_errors"),
            InlineKeyboardButton(_("⌨️ All commands"), callback_data="menu_commands"),
        ],
        menu_exit_row(),
    ]
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return True


async def _handle_menu_errors(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> bool:
    """The read-failure report, the same one /errors sends."""
    from price_tracker.bot.handlers.debug import render_errors  # noqa: PLC0415 — cycle

    await query.edit_message_text(
        await render_errors(db, context.bot_data.get("health_manager"), user_id),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
    )
    return True


async def _handle_menu_commands(query: Any) -> bool:
    """The full command list.

    Telegram's own command menu is a flat list with no submenus, so it publishes
    only the handful of commands worth a tap. Everything else still works when
    typed — this is where to find out that it exists.
    """
    from price_tracker.bot.commands import ADMIN_COMMANDS, USER_COMMANDS  # noqa: PLC0415

    lines = [_("⌨️ <b>All commands</b>"), ""]
    lines += [f"{icon} /{name} — {_(text)}" for name, icon, text in USER_COMMANDS]
    lines += ["", _("<b>Admin</b>")]
    lines += [f"{icon} /{name} — {_(text)}" for name, icon, text in ADMIN_COMMANDS]
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([menu_exit_row()]),
    )
    return True
