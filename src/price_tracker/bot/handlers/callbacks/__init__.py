"""Inline-button callback dispatcher.

The original `handle_callback` was a single ~700-LOC if/elif chain. Task 17
splits it into per-domain helpers under this package and keeps the
dispatcher itself a thin sequence of `if handled := await ...: return`
calls.

The dispatcher also owns the navigation trail. Recording "this token rendered
this message" centrally is what lets every screen offer a working ◀️ Back without
knowing which of its several callers it was opened from — the trail knows, and no
screen has to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from telegram.ext import Application, CallbackQueryHandler

from price_tracker.bot.decorators import _db, with_locale
from price_tracker.bot.handlers.callbacks import (
    _actions,
    _admin,
    _delivery,
    _groups,
    _list,
    _menu,
    _nav,
    _ops,
    _picker,
    _product,
)
from price_tracker.bot.navigation import push_nav, restore_nav, snapshot_nav

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Screens that manage their own trail: opening a chart replaces the panel with a
# photo message, so the trail moves rather than growing.
_SELF_NAVIGATING = ("chart_", "grp_chart_")


async def _dispatch(
    query: Any, context: ContextTypes.DEFAULT_TYPE, db: Any, user_id: int, data: str
) -> bool:
    """Offer `data` to each per-domain helper until one claims it."""
    # Order matters — earlier handlers have higher specificity. Each helper
    # returns True when it handled the callback; on False we fall through.
    return (
        await _ops.handle_ops_buttons(query, context, db, user_id, data)
        or await _list.handle_list_navigation(query, context, db, user_id, data)
        or await _product.handle_delete_flow(query, context, db, user_id, data)
        or await _product.handle_check_button(query, context, db, user_id, data)
        or await _product.handle_chart_button(query, context, db, user_id, data)
        or await _product.handle_amazon_pref(query, context, db, user_id, data)
        or await _product.handle_track_choice(query, context, db, user_id, data)
        or await _actions.handle_edit_button(query, context, db, user_id, data)
        or await _actions.handle_product_groups_button(query, context, db, user_id, data)
        or await _actions.handle_pause_button(query, context, db, user_id, data)
        or await _actions.handle_remove_button(query, context, db, user_id, data)
        or await _actions.handle_reset_button(query, context, db, user_id, data)
        or await _actions.handle_reactivate_button(query, context, db, user_id, data)
        or await _menu.handle_menu_navigation(query, context, db, user_id, data)
        or await _delivery.handle_delivery_menu(query, context, db, user_id, data)
        or await _groups.handle_group_buttons(query, context, db, user_id, data)
        or await _admin.handle_admin_menu(query, context, db, user_id, data)
        or await _picker.handle_picker_page(query, context, db, user_id, data)
        or await _actions.handle_picker(query, context, db, user_id, data)
    )


@with_locale
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch every inline-button click to the matching per-domain helper."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db = _db(context)
    if not await db.is_user_allowed(user_id):
        return

    data = query.data
    message_id = getattr(getattr(query, "message", None), "message_id", None)

    if await _nav.handle_close(query, context, data):
        return
    if await _nav.handle_cancel(query, context, data):
        return
    if await _nav.handle_back(query, context, db, user_id, data, _dispatch):
        return

    # A panel sent before the menu was reordered still carries the old token.
    # Rewritten here rather than in the menu handler so it reaches whichever
    # screen now owns it — the listing, in most cases, which the menu does not
    # render itself.
    data = _menu.RETIRED_SCREENS.get(data, data)

    # Pushed before the screen renders, so a screen drawing itself always finds
    # itself on top of the trail and the screen behind it one below — which is
    # what `nav_row` needs to decide whether ◀️ Back leads anywhere. A chart
    # carries this entry over to the photo it replaces the panel with.
    trail = snapshot_nav(context, message_id) if message_id is not None else []
    if message_id is not None:
        push_nav(context, message_id, data)

    if await _dispatch(query, context, db, user_id, data):
        return

    if message_id is not None:
        restore_nav(context, message_id, trail)
    logger.info("Unhandled callback data: %s", data)


def register(app: Application) -> None:
    """Register the inline-button callback dispatcher on `app`."""
    app.add_handler(CallbackQueryHandler(handle_callback))


__all__ = ["handle_callback", "register"]
