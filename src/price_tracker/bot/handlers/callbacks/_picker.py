"""Turning the page in a product picker.

A picker's buttons are the action itself — `check_3`, `settarget_3`,
`grp_put_7_3` — so the only thing this adds is the page. The token carries the
action prefix so a page turn knows which picker it is redrawing, and the list is
rebuilt from that prefix rather than remembered: a picker opened ten minutes ago
must not offer a product deleted since.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from price_tracker.bot.handlers._helpers import _parse_id
from price_tracker.bot.handlers.product_list import build_picker_view
from price_tracker.bot.keyboards import PICKER_PREFIX
from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

# Prefix → the title its screen carries. The msgids are the ones the commands
# already used, so none of this needed translating again.
_TITLES: dict[str, str] = {
    "check_": "Choose product to check",
    "pause_": "Choose product to pause",
    "setrefresh_": "Choose product to set interval",
    "settarget_": "Pick a product to set a target for",
    "setsoglia_": "Pick a product to set a threshold for",
    "chart_": "Pick a product to see its history",
}


async def resolve_picker(db: Any, user_id: int, prefix: str) -> tuple[str, list[Any]] | None:
    """The title and the products a picker shows, worked out from its prefix.

    None when the prefix belongs to no picker — a tampered or retired token.
    """
    if prefix in _TITLES:
        return _(_TITLES[prefix]), list(await db.get_active_products(user_id))

    for action, member in (("grp_put_", False), ("grp_pull_", True)):
        if not prefix.startswith(action):
            continue
        group_id = _parse_id(prefix.removeprefix(action).rstrip("_"))
        if group_id is None:
            return None
        group = await db.get_group(group_id, user_id=user_id)
        if group is None:
            return None
        members = list(await db.list_group_products(group_id, user_id=user_id))
        if member:
            return _("➖ {name} — pick a product to remove").format(name=group.name), members
        ids = {int(p["id"]) for p in members}
        candidates = [p for p in await db.get_active_products(user_id) if int(p["id"]) not in ids]
        return _("➕ {name} — pick a product to add").format(name=group.name), candidates

    return None


async def handle_picker_page(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Redraw a picker at another page. True when handled."""
    if not data.startswith(PICKER_PREFIX):
        return False

    prefix, _sep, raw_page = data.removeprefix(PICKER_PREFIX).partition("|")
    page = _parse_id(raw_page)
    resolved = await resolve_picker(db, user_id, prefix)
    if resolved is None:
        await query.edit_message_text(_("❌ That list is no longer available."))
        return True

    title, products = resolved
    message_id = getattr(getattr(query, "message", None), "message_id", None)
    text, keyboard = build_picker_view(
        products,
        max(0, page or 0),
        prefix=prefix,
        title=title,
        context=context,
        message_id=message_id,
    )
    from telegram.constants import ParseMode  # noqa: PLC0415 — one call site

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return True
