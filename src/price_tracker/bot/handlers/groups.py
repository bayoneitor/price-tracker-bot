"""`/groups` — named sets of products, and the comparisons between them.

Tracking three monitors told you each one's price and nothing about how they
stood against each other. A group answers that: which is cheapest now, how far
apart they are, and — from the price history already on disk — which one has been
cheapest over time.

Rendering lives in `groups_view` as pure functions, so the command and the
callbacks draw the same panels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler

from price_tracker.bot.decorators import _db, restricted, with_locale
from price_tracker.bot.handlers.groups_view import build_groups_list
from price_tracker.bot.navigation import push_nav

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


@with_locale
@restricted
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's groups as one panel."""
    groups = await _db(context).list_groups(user_id=update.effective_user.id)
    text, keyboard = build_groups_list(groups)
    message = await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    if context.user_data is not None:
        # Seeds the trail so everything opened from here has a working ◀️ Back.
        push_nav(context, message.message_id, "menu_groups")


def register(app: Application) -> None:
    """Register the group command handlers on `app`."""
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CommandHandler("grupos", cmd_groups))
    app.add_handler(CommandHandler("gruppi", cmd_groups))
