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
    "alias": PendingSpec(needs_product=True, label=N_("a name for the product")),
    "group_new": PendingSpec(label=N_("a name for the group")),
    "group_rename": PendingSpec(label=N_("a new name for the group")),
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
# The dispatcher pushes the token *before* the screen renders, so while a screen is
# drawing itself the top of its trail is always that screen and the one below is
# always where "back" leads. Pushing afterwards looked equivalent and was not:
# re-rendering the same screen (paging a listing) left the screen itself on top, so
# a listing grew a "back" button the moment it was paged — one that led nowhere —
# and every button in that row shifted position.

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

    The top of the trail is the screen currently drawing itself, so the answer is
    the one below it.
    """
    stack = _stack(context, message_id)
    return stack[-2] if len(stack) >= 2 else None


def pop_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> str | None:
    """Leave the current screen and return the token to render instead.

    Only the screen being left comes off; the one returned stays on top, because
    the caller renders it directly rather than going back through the dispatcher.
    """
    stack = _stack(context, message_id)
    if len(stack) < 2:
        stack.clear()
        return None
    stack.pop()
    return stack[-1]


def snapshot_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> list[str]:
    """A copy of a message's trail, for the dispatcher to restore on a miss."""
    return list(_stack(context, message_id))


def restore_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int, snapshot: list[str]) -> None:
    """Put a trail back, after pushing a token that turned out to render nothing."""
    stack = _stack(context, message_id)
    stack[:] = snapshot


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


# Every screen that draws one product, by the token that renders it. A trail
# outlives what it points at, so these are what has to come out when the product
# goes — see `forget_product`. Extend this when a new per-product screen appears:
# a prefix missing here costs one "❌ Product not found." before `resolve_owned_product`
# purges it anyway, rather than stranding the reader.
PRODUCT_SCREEN_PREFIXES = (
    "prod_",
    "edit_",
    "remove_",
    "confirm_delete_",
    "pause_",
    "check_",
    "chart_",
    "reset_",
    "reactivate_",
    "grp_of_",
    "settarget_",
    "setsoglia_",
    "setrefresh_",
    "setalias_",
    "track_any_",
    "track_target_",
    "track_threshold_",
    "track_default_",
    "pref_new_",
    "pref_used_",
    "pref_amazon_",
    "pref_anyseller_",
    "pref_default_",
)


def forget_product(context: ContextTypes.DEFAULT_TYPE, product_id: int) -> None:
    """Drop every trail entry that renders a product which no longer exists.

    "Back" re-dispatches a stored token, so a trail outlives its subject.
    Deleting a product left `prod_5`, `remove_5` and `confirm_delete_5` sitting in
    the trail of the very message that had just confirmed the deletion, and the
    next ◀️ Back rendered "❌ Product not found." — the bot contradicting what it
    had told the reader one tap earlier.

    Every trail is swept, not just the one in front of the reader: the same
    product may be open in another panel further up the chat.
    """
    if context.user_data is None:
        return
    dead = {f"{prefix}{product_id}" for prefix in PRODUCT_SCREEN_PREFIXES}
    trails: dict[int, list[str]] = context.user_data.get(NAV_KEY, {})
    for stack in trails.values():
        stack[:] = [token for token in stack if token not in dead]


def forget_nav(context: ContextTypes.DEFAULT_TYPE, message_id: int) -> None:
    """Drop a closed message's trail so user_data does not grow forever."""
    if context.user_data is None:
        return
    trails: dict[int, list[str]] = context.user_data.get(NAV_KEY, {})
    trails.pop(message_id, None)
