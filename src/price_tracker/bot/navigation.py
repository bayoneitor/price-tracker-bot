"""Conversational state: what the bot is waiting for the user to type.

`handle_text_input` used to receive a bare `("target", 42)` tuple and had no way
to tell one kind of prompt from another. Two defects came straight out of that:

- Every prompt looked up a *product* before branching, so the admin prompts —
  whose second slot holds a user id, or nothing at all — answered "Product not
  found." and did nothing.
- Nothing recorded whether a prompt legitimately expects a URL, so the paste-a-link
  shortcut hijacked the one prompt (`admin_debug`) that asks for a URL and added
  it as a tracked product instead.

`PendingInput` carries the answers to both questions, plus the id of the message
that asked, so the reply can be shown by editing the question instead of piling a
new message onto the chat.

State lives in `context.user_data`, which is in-memory: a restart forgets any
prompt that was open, and the user simply asks again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from price_tracker.bot.messages import N_

if TYPE_CHECKING:
    from telegram import Message
    from telegram.ext import ContextTypes

PENDING_KEY = "pending_action"


@dataclass(frozen=True, slots=True)
class PendingSpec:
    """What a prompt expects, so the router can treat each kind correctly."""

    #: Resolve `target_id` as a product owned by the caller before running.
    needs_product: bool = False
    #: A URL is a legitimate answer here, not a request to track a new product.
    accepts_url: bool = False
    #: msgid describing the expected answer, for the "I'm still waiting for …" notice.
    label: str = ""


PENDING_SPECS: dict[str, PendingSpec] = {
    "target": PendingSpec(needs_product=True, label=N_("a target price")),
    "threshold": PendingSpec(needs_product=True, label=N_("a price-drop threshold")),
    "refresh": PendingSpec(needs_product=True, label=N_("a check interval in minutes")),
    "admin_adduser": PendingSpec(label=N_("a Telegram user id")),
    "admin_nick": PendingSpec(label=N_("a nickname")),
    "admin_interval": PendingSpec(label=N_("a check interval in minutes")),
    "admin_debug": PendingSpec(accepts_url=True, label=N_("a product URL")),
    "quiet_hours": PendingSpec(label=N_("quiet hours as HH:MM-HH:MM")),
    "timezone": PendingSpec(label=N_("a timezone name")),
    "throttle": PendingSpec(label=N_("how many alerts per hour")),
    "digest_interval": PendingSpec(label=N_("a digest interval in minutes")),
}


@dataclass(frozen=True, slots=True)
class PendingInput:
    """A prompt awaiting a typed answer.

    `target_id` means whatever the action needs — a product id for the per-product
    prompts, a user id for `admin_nick`, nothing (0) for the rest. `PendingSpec`
    says which, so no branch has to guess.
    """

    action: str
    target_id: int = 0
    prompt_message_id: int | None = None
    chat_id: int | None = None

    @property
    def spec(self) -> PendingSpec:
        return PENDING_SPECS[self.action]


def set_pending(
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    target_id: int = 0,
    *,
    message: Message | None = None,
) -> PendingInput:
    """Arm a prompt. `message` is the message doing the asking, so the answer can
    be rendered by editing it rather than by sending another one."""
    if action not in PENDING_SPECS:
        raise KeyError(f"unknown pending action: {action}")
    pending = PendingInput(
        action=action,
        target_id=target_id,
        prompt_message_id=getattr(message, "message_id", None),
        chat_id=getattr(getattr(message, "chat", None), "id", None),
    )
    if context.user_data is not None:
        context.user_data[PENDING_KEY] = pending
    return pending


def get_pending(context: ContextTypes.DEFAULT_TYPE) -> PendingInput | None:
    """Return the open prompt, or None. Anything else stored under the key — an
    unknown action left by an older build — is discarded rather than crashed on."""
    if context.user_data is None:
        return None
    value: Any = context.user_data.get(PENDING_KEY)
    if isinstance(value, PendingInput) and value.action in PENDING_SPECS:
        return value
    if value is not None:
        context.user_data.pop(PENDING_KEY, None)
    return None


def clear_pending(context: ContextTypes.DEFAULT_TYPE) -> PendingInput | None:
    """Close the open prompt and return it, if there was one."""
    pending = get_pending(context)
    if context.user_data is not None:
        context.user_data.pop(PENDING_KEY, None)
    return pending


# ── Where "back" goes ─────────────────────────────────────────────
#
# Panels are rendered by editing one message, and the same panel is reachable
# from several places — the edit panel opens from the listing, from Menu →
# Products and from Menu → Notifications — so no screen can name its own parent.
# Each message therefore keeps the trail of callback tokens that rendered it, and
# "back" re-dispatches the previous one. Tokens, not rendered screens: replaying
# `list_go_2` re-reads the products, so going back never shows a stale price.
#
# The dispatcher does the pushing, once, for every screen; a screen only has to
# ask `nav_row()` for the button.

NAV_KEY = "nav"

# Deep enough for any real path through the menus, shallow enough that an
# abandoned message cannot grow without bound.
NAV_DEPTH = 8


def _family(token: str) -> str:
    """Which screen a token renders, ignoring which page of it.

    Paging through a listing re-renders the same screen, so the pages must
    collapse onto one stack entry — otherwise "back" would walk the user through
    every page turn before leaving the listing.
    """
    for prefix in ("list_go_", "grp_go_"):
        if token.startswith(prefix):
            return prefix
    return token


def _stack(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> list[str]:
    if context.user_data is None:
        return []
    trails: dict[int, list[str]] = context.user_data.setdefault(NAV_KEY, {})
    return trails.setdefault(message_id, [])


def push_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int, token: str) -> None:
    """Record that `token` rendered what is now on `message_id`."""
    stack = _stack(context, message_id)
    if stack and _family(stack[-1]) == _family(token):
        stack[-1] = token
        return
    stack.append(token)
    del stack[:-NAV_DEPTH]


def previous_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> str | None:
    """The screen a "back" button would return to, or None if there is none.

    Called while building a keyboard — before the dispatcher pushes the screen
    being built — so the top of the stack is the screen the user came from.
    """
    stack = _stack(context, message_id)
    return stack[-1] if stack else None


def pop_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> str | None:
    """Leave the current screen and return the token to render instead.

    Both entries come off: the caller re-dispatches the returned token through
    the normal path, which pushes it back on.
    """
    stack = _stack(context, message_id)
    if len(stack) < 2:
        stack.clear()
        return None
    stack.pop()
    return stack.pop()


def transfer_nav(context: ContextTypes.DEFAULT_TYPE, from_id: int, to_id: int) -> None:
    """Move a trail onto another message.

    Telegram cannot turn a text message into a photo, so opening a chart replaces
    the panel with a new message; the trail has to follow it or "back" would have
    nowhere to go.
    """
    if context.user_data is None:
        return
    trails: dict[int, list[str]] = context.user_data.setdefault(NAV_KEY, {})
    trail = trails.pop(from_id, None)
    if trail is not None:
        trails[to_id] = trail


def forget_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> None:
    """Drop a closed message's trail so user_data does not grow forever."""
    if context.user_data is None:
        return
    trails: dict[int, list[str]] = context.user_data.get(NAV_KEY, {})
    trails.pop(message_id, None)
