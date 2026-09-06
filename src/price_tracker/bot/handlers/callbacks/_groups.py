"""Group callbacks: membership, comparison, and the comparison's history.

Every handler takes the caller's user_id straight through to the repository,
which filters on it. Group ids travel in callback data as plain integers, so an
unfiltered lookup would let anyone open, edit or delete anyone's group by
guessing a number — the same hole that had to be closed on the product buttons.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError

from price_tracker.bot.charts import MAX_SERIES, generate_comparison_chart
from price_tracker.bot.handlers._helpers import _escape_html, _parse_id
from price_tracker.bot.handlers.groups_view import (
    GROUP_OPEN_PREFIX,
    build_comparison_table,
    build_group_view,
    build_groups_list,
    build_leader_timeline,
    render_leader_timeline,
)
from price_tracker.bot.keyboards import prompt_keyboard, result_keyboard
from price_tracker.bot.messages import _
from price_tracker.bot.navigation import set_pending, transfer_nav
from price_tracker.core.textlimits import truncate_visible

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _message_id(query: Any) -> int | None:
    return getattr(getattr(query, "message", None), "message_id", None)


def _two_ids(data: str, prefix: str) -> tuple[int, int] | None:
    """Parse a `<prefix><group>_<product>` callback."""
    parts = data.removeprefix(prefix).split("_")
    if len(parts) != 2:
        return None
    group_id, product_id = _parse_id(parts[0]), _parse_id(parts[1])
    if group_id is None or product_id is None:
        return None
    return group_id, product_id


async def handle_group_buttons(  # noqa: PLR0911 — one branch per button, each short
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Dispatch every `grp_*` button, plus the menu entry that lists groups."""
    if data == "menu_groups":
        await _show_groups(query, context, db, user_id)
        return True

    if data == "grp_new":
        set_pending(context, "group_new", message=query.message)
        await query.edit_message_text(
            _("🏷 <b>New group</b>\n\nType a name for it, e.g. <code>Monitors</code>:"),
            parse_mode=ParseMode.HTML,
            reply_markup=prompt_keyboard(context, _message_id(query)),
        )
        return True

    if data.startswith(GROUP_OPEN_PREFIX):
        return await _open(query, context, db, user_id, data.removeprefix(GROUP_OPEN_PREFIX))

    for prefix, handler in _GROUP_ACTIONS.items():
        if data.startswith(prefix):
            group_id = _parse_id(data.removeprefix(prefix))
            if group_id is None:
                await query.edit_message_text(_("❌ Invalid ID."))
                return True
            group = await db.get_group(group_id, user_id=user_id)
            if group is None:
                await query.edit_message_text(_("❌ Group not found."))
                return True
            return await handler(query, context, db, user_id, group)

    for prefix, membership in (("grp_put_", True), ("grp_pull_", False)):
        if data.startswith(prefix):
            return await _set_membership(query, context, db, user_id, data, prefix, membership)

    return False


# ── Panels ───────────────────────────────────────────────────────────────


async def _show_groups(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int
) -> None:
    groups = await db.list_groups(user_id=user_id)
    text, keyboard = build_groups_list(groups, context=context, message_id=_message_id(query))
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def _open(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, raw_id: str
) -> bool:
    group_id = _parse_id(raw_id)
    if group_id is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    group = await db.get_group(group_id, user_id=user_id)
    if group is None:
        await query.edit_message_text(_("❌ Group not found."))
        return True
    await _render_group(query, context, db, user_id, group)
    return True


async def _render_group(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> None:
    products = await db.list_group_products(group.id, user_id=user_id)
    text, keyboard = build_group_view(
        group, products, context=context, message_id=_message_id(query)
    )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ── Actions on one group ─────────────────────────────────────────────────


async def _compare(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    products = await db.list_group_products(group.id, user_id=user_id)
    await query.edit_message_text(
        build_comparison_table(products),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query)),
    )
    return True


async def _leaders(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    """Who has been cheapest, and since when — read out of the price history."""
    products = await db.list_group_products(group.id, user_id=user_id)
    ids = [int(p["id"]) for p in products]
    histories = await db.get_price_history_for_products(ids) if ids else {}
    names = {int(p["id"]): (p.get("name") or _("Unknown")) for p in products}
    await query.edit_message_text(
        render_leader_timeline(build_leader_timeline(histories), names),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query)),
    )
    return True


async def _chart(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    """Draw the group on one chart, replacing the panel with the photo.

    Same shape as the single-product chart: Telegram will not edit a text message
    into a photo, so the panel is deleted and the trail moves to the image, which
    is what lets ◀️ Back reopen the group afterwards.
    """
    products = await db.list_group_products(group.id, user_id=user_id)
    origin_id = _message_id(query)
    chart = await generate_comparison_chart(db, products, group.name)
    if chart is None:
        await query.edit_message_text(
            _("📭 Not enough history yet — at least two products need two readings each."),
            reply_markup=result_keyboard(context, origin_id),
        )
        return True

    caption = _("📈 <b>{name}</b> — {count} products").format(
        name=_escape_html(truncate_visible(group.name, 40)),
        count=min(len(products), MAX_SERIES),
    )
    if len(products) > MAX_SERIES:
        caption += _("\n(showing the first {count})").format(count=MAX_SERIES)

    keyboard = result_keyboard(context, origin_id)
    with contextlib.suppress(TelegramError):
        await query.message.delete()
    photo = await query.message.reply_photo(
        photo=InputFile(chart, filename=f"group_{group.id}.png"),
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    if origin_id is not None:
        transfer_nav(context, origin_id, photo.message_id)
    return True


async def _rename(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    set_pending(context, "group_rename", group.id, message=query.message)
    await query.edit_message_text(
        _("✏️ <b>{name}</b>\n\nType the new name:").format(
            name=_escape_html(truncate_visible(group.name, 40))
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=prompt_keyboard(context, _message_id(query)),
    )
    return True


async def _confirm_delete(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    await query.edit_message_text(
        _(
            "🗑 Delete the group <b>{name}</b>?\n\n"
            "The {count} products in it stay tracked — only the grouping goes."
        ).format(name=_escape_html(truncate_visible(group.name, 40)), count=group.member_count),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(
            context,
            _message_id(query),
            [
                InlineKeyboardButton(
                    _("🗑 Yes, delete the group"), callback_data=f"grp_delok_{group.id}"
                )
            ],
        ),
    )
    return True


async def _delete(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    await db.delete_group(group.id, user_id=user_id)
    await _show_groups(query, context, db, user_id)
    return True


async def _add_picker(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    """Offer the products that are not in this group yet."""
    members = {int(p["id"]) for p in await db.list_group_products(group.id, user_id=user_id)}
    candidates = [p for p in await db.get_active_products(user_id) if int(p["id"]) not in members]
    if not candidates:
        await query.edit_message_text(
            _("📭 Every tracked product is already in this group."),
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return True
    rows = [
        [
            InlineKeyboardButton(
                f"#{p['id']} {truncate_visible(p.get('name') or '?', 32)}",
                callback_data=f"grp_put_{group.id}_{p['id']}",
            )
        ]
        for p in candidates[:20]
    ]
    await query.edit_message_text(
        _("➕ <b>{name}</b>\n\nPick a product to add:").format(
            name=_escape_html(truncate_visible(group.name, 40))
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query), *rows),
    )
    return True


async def _remove_picker(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, group: Any
) -> bool:
    members = await db.list_group_products(group.id, user_id=user_id)
    if not members:
        await query.edit_message_text(
            _("📭 Nothing in this group yet."),
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return True
    rows = [
        [
            InlineKeyboardButton(
                f"➖ #{p['id']} {truncate_visible(p.get('name') or '?', 30)}",
                callback_data=f"grp_pull_{group.id}_{p['id']}",
            )
        ]
        for p in members[:20]
    ]
    await query.edit_message_text(
        _("➖ <b>{name}</b>\n\nPick a product to remove:").format(
            name=_escape_html(truncate_visible(group.name, 40))
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query), *rows),
    )
    return True


async def _set_membership(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    db: Any,
    user_id: int,
    data: str,
    prefix: str,
    join: bool,
) -> bool:
    ids = _two_ids(data, prefix)
    if ids is None:
        await query.edit_message_text(_("❌ Invalid ID."))
        return True
    group_id, product_id = ids
    if join:
        await db.add_to_group(group_id, product_id, user_id=user_id)
    else:
        await db.remove_from_group(group_id, product_id, user_id=user_id)

    group = await db.get_group(group_id, user_id=user_id)
    if group is None:
        await query.edit_message_text(_("❌ Group not found."))
        return True
    await _render_group(query, context, db, user_id, group)
    return True


async def show_product_groups(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, product_id: int
) -> None:
    """The '🏷 Groups' panel on a product: every group, ticked where it belongs."""
    groups = await db.list_groups(user_id=user_id)
    member_ids = {g.id for g in await db.list_groups_for_product(product_id, user_id=user_id)}
    if not groups:
        await query.edit_message_text(
            _("🏷 You have no groups yet. Create one from /groups."),
            reply_markup=result_keyboard(context, _message_id(query)),
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if group.id in member_ids else "➕ ") + truncate_visible(group.name, 30),
                callback_data=(
                    f"grp_pull_{group.id}_{product_id}"
                    if group.id in member_ids
                    else f"grp_put_{group.id}_{product_id}"
                ),
            )
        ]
        for group in groups
    ]
    await query.edit_message_text(
        _("🏷 <b>Groups for #{pid}</b>\n\nTap to add or remove.").format(pid=product_id),
        parse_mode=ParseMode.HTML,
        reply_markup=result_keyboard(context, _message_id(query), *rows),
    )


_GROUP_ACTIONS = {
    "grp_cmp_": _compare,
    "grp_chart_": _chart,
    "grp_lead_": _leaders,
    "grp_ren_": _rename,
    "grp_delok_": _delete,
    "grp_del_": _confirm_delete,
    "grp_add_": _add_picker,
    "grp_rem_": _remove_picker,
}

__all__ = ["handle_group_buttons", "show_product_groups"]
