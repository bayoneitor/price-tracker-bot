"""Reusable inline-keyboard builders for the bot UI.

Ported from monolithic bot.py [Task 17].

Every screen that does something ends with a navigation row built by
:func:`nav_row`, so there is one place that decides what an exit looks like and
no screen can ship without one — which is how the product pickers, the typed
prompts and the result screens all ended up with no way out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from price_tracker.bot.messages import _
from price_tracker.bot.navigation import previous_nav

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

# Any view that can be dismissed uses this one callback; the handler deletes
# whichever message carries the button, so it needs no per-view variants.
CLOSE_CALLBACK = "close_view"

# Return to the screen this one was opened from, whatever that was.
BACK_CALLBACK = "nav_back"

# Abandon whatever the bot is waiting for the user to type.
CANCEL_CALLBACK = "cancel_action"

# The paginated listing's page token. It lives here rather than with the view
# because the main menu opens the listing directly, and a keyboard module cannot
# import a handler that already imports it.
LIST_GOTO_PREFIX = "list_go_"

# One product's own screen, opened from the index.
PRODUCT_PREFIX = "prod_"

# Turn a page in a product picker: `pick|<action prefix>|<page>`. A pipe separates
# because the action prefixes already carry underscores (`grp_put_7_`), which
# leaves no unambiguous place to split on.
PICKER_PREFIX = "pick|"

# Move the index's cursor one product at a time. Separate from LIST_GOTO_PREFIX,
# which moves a whole page: two speeds, two rows of buttons, so neither is
# mistaken for the other.
LIST_CURSOR_PREFIX = "list_cur_"


def close_button() -> InlineKeyboardButton:
    """The '✖ Close' button, built under the caller's locale."""
    return InlineKeyboardButton(_("✖ Close"), callback_data=CLOSE_CALLBACK)


def back_button() -> InlineKeyboardButton:
    """The '◀️ Back' button — returns to the screen this one was opened from."""
    return InlineKeyboardButton(_("◀️ Back"), callback_data=BACK_CALLBACK)


def cancel_button() -> InlineKeyboardButton:
    """The '✖ Cancel' button — drops whatever answer the bot is waiting for."""
    return InlineKeyboardButton(_("✖ Cancel"), callback_data=CANCEL_CALLBACK)


def nav_row(
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int | None,
    *,
    back: bool = True,
    cancel: bool = False,
    close: bool = True,
) -> list[InlineKeyboardButton]:
    """The exit row for a screen: back where there is somewhere to go, then out.

    "Back" is omitted when this message has no trail behind it — a first screen
    showing a button that leads nowhere is worse than showing none.
    """
    row: list[InlineKeyboardButton] = []
    if back and message_id is not None and previous_nav(context, message_id) is not None:
        row.append(back_button())
    if cancel:
        row.append(cancel_button())
    if close:
        row.append(close_button())
    return row


def prompt_keyboard(
    context: ContextTypes.DEFAULT_TYPE, message_id: int | None
) -> InlineKeyboardMarkup:
    """The exit row for a screen waiting for the user to type something.

    Always at least a cancel: a prompt the user cannot back out of is one they can
    only escape by guessing a magic word, which is what these used to be.
    """
    row = nav_row(context, message_id, cancel=True, close=False) or [cancel_button()]
    return InlineKeyboardMarkup([row])


def result_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int | None,
    *rows: list[InlineKeyboardButton],
) -> InlineKeyboardMarkup:
    """A screen that has finished doing something: its own buttons, then the way out."""
    exits = nav_row(context, message_id) or [close_button()]
    return InlineKeyboardMarkup([*rows, exits])


def build_threshold_keyboard(
    product_id: int,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Build the standard threshold/notification choice keyboard."""
    rows = [
        [
            InlineKeyboardButton(
                _("\U0001f514 Every drop"),
                callback_data=f"track_any_{product_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                _("\U0001f4c9 Threshold % or €"),
                callback_data=f"track_threshold_{product_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                _("\U0001f4b0 Target price"),
                callback_data=f"track_target_{product_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                _("\U0001f44d -10% is fine (default)"),
                callback_data=f"track_default_{product_id}",
            ),
        ],
    ]
    if context is not None:
        exits = nav_row(context, message_id)
        if exits:
            rows.append(exits)
    return InlineKeyboardMarkup(rows)


def build_main_menu(is_admin: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """The main menu, built once for both the places that show it.

    `/menu` sends it and the ◀️ Menu button re-renders it, and those were two
    separate implementations that had drifted apart: two columns against one, and
    a different label on all but one entry. Going back to the menu rearranged it
    under the reader.
    """
    # Products opens the real listing rather than a screen that imitates it. The
    # menu used to hold four separate "pick a product" pickers — products, prices,
    # history, notifications — each capped at eight or ten and none of them able to
    # page, jump or close, while the listing that could do all of it was only
    # reachable once you had more than ten products.
    rows = [
        [
            InlineKeyboardButton(_("📦 Products"), callback_data=f"{LIST_GOTO_PREFIX}0"),
            InlineKeyboardButton(_("🏷 Groups"), callback_data="menu_groups"),
        ],
        [
            InlineKeyboardButton(_("🔍 Check all prices"), callback_data="menu_checkall"),
            InlineKeyboardButton(_("🔔 Notifications"), callback_data="menu_delivery"),
        ],
        [
            InlineKeyboardButton(_("💾 Data"), callback_data="menu_dati"),
            InlineKeyboardButton(_("📊 Status and info"), callback_data="menu_info"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(_("👑 Admin"), callback_data="menu_admin")])
    return _("📋 <b>Menu</b>\n\nWhat do you want to do?"), InlineKeyboardMarkup(rows)


def menu_back_button() -> list[InlineKeyboardButton]:
    """Single-row 'back to main menu' button."""
    return [InlineKeyboardButton(_("◀️ Menu"), callback_data="menu_main")]


def menu_exit_row() -> list[InlineKeyboardButton]:
    """The exit row for a menu screen: up to the main menu, or out altogether.

    Menu screens hang off one fixed parent, so they name it directly instead of
    consulting the navigation trail.
    """
    return [*menu_back_button(), close_button()]
