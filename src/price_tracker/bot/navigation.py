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
