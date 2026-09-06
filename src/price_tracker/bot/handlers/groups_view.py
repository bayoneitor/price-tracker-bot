"""Rendering for product groups and the comparison between their members.

Pure functions of their inputs, like `product_list.build_list_view`: the command
and the callbacks build the same view, and the tests can assert on it without a
Telegram round trip.

The comparison's history — who was cheapest, and since when — is derived from
`price_history` rather than stored. Nothing has to be recorded in advance, so a
group created today can still be compared over the months its members have
already been tracked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from price_tracker.bot.handlers._helpers import _escape_html, _safe_dec
from price_tracker.bot.keyboards import close_button, nav_row
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _
from price_tracker.core.textlimits import truncate_visible

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from telegram.ext import ContextTypes

GROUP_OPEN_PREFIX = "grp_open_"
GROUP_GOTO_PREFIX = "grp_go_"
GROUP_NAME_BUDGET = 32


# ── Who was cheapest, and since when ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LeaderSpan:
    """A stretch of time during which one product was the cheapest in its group."""

    since: datetime
    product_id: int
    price: Decimal
    tied_with: tuple[int, ...] = ()


def _as_datetime(value: str) -> datetime | None:
    """Parse a stored timestamp. The DB writes them naive; comparison only needs order."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def build_leader_timeline(
    histories: Mapping[int, Sequence[Any]],
) -> list[LeaderSpan]:
    """When the cheapest product in the group changed, and to what.

    Every reading from every member is replayed in time order while the last known
    price of each is carried forward — the members are not checked in lockstep, so
    at any instant most of them have no reading of their own and comparing only
    same-timestamp readings would compare almost nothing.

    Ties resolve to the lowest product id and are reported, so "they cost the same"
    never reads as a leadership change back and forth.
    """
    events: list[tuple[datetime, int, Decimal]] = []
    for product_id, records in histories.items():
        for record in records:
            when = _as_datetime(record.get("checked_at") if hasattr(record, "get") else None)
            price = _safe_dec(record.get("price") if hasattr(record, "get") else None)
            if when is not None and price is not None:
                events.append((when, product_id, price))
    events.sort(key=lambda e: (e[0].timestamp(), e[1]))

    spans: list[LeaderSpan] = []
    latest: dict[int, Decimal] = {}
    for when, product_id, price in events:
        latest[product_id] = price
        best = min(latest.values())
        winners = tuple(sorted(pid for pid, value in latest.items() if value == best))
        leader = winners[0]
        if spans and spans[-1].product_id == leader:
            # The lead did not change hands. Its price moving is not an event —
            # recording every tick of the cheapest product buried the handful of
            # entries that actually answer "who has been cheapest".
            if spans[-1].tied_with != winners[1:]:
                spans[-1] = replace(spans[-1], tied_with=winners[1:])
            continue
        spans.append(LeaderSpan(since=when, product_id=leader, price=best, tied_with=winners[1:]))
    return spans


def render_leader_timeline(
    spans: Sequence[LeaderSpan], names: Mapping[int, str], *, limit: int = 12
) -> str:
    """The leader timeline as text, most recent last."""
    if not spans:
        return _("📭 Not enough history yet to say who has been cheapest.")

    lines = [_("🕐 <b>Who has been cheapest</b>"), ""]
    shown = spans[-limit:]
    if len(spans) > len(shown):
        lines.append(
            _("   … {count} earlier changes not shown").format(count=len(spans) - len(shown))
        )
    for span in shown:
        # `names` is already budgeted by the caller — re-truncating here would cut
        # the shop off the end, which is the half that tells two rows apart.
        name = _escape_html(names.get(span.product_id, "?"))
        line = _("{date} — {name} at €{price:.2f}").format(
            date=span.since.strftime("%d/%m"), name=name, price=span.price
        )
        if span.tied_with:
            line += _(" (tied with {count} more)").format(count=len(span.tied_with))
        lines.append(line)
    return "\n".join(lines)


# ── The side-by-side table ───────────────────────────────────────────────


def build_comparison_table(products: Sequence[dict[str, Any]]) -> str:
    """Every member's price side by side, cheapest first, with the spread."""
    if len(products) < 2:
        return _("📭 Add at least two products to compare them.")

    priced = [(p, _safe_dec(p.get("current_price"))) for p in products]
    known = [(p, price) for p, price in priced if price is not None]
    lines = [_("📊 <b>Comparison</b>"), ""]

    for product, price in sorted(known, key=lambda row: row[1]):
        name = _escape_html(product_label(product))
        line = f"<b>#{product['id']}</b> {name} — €{price:.2f}"
        initial = _safe_dec(product.get("initial_price"))
        if initial and initial > 0 and initial != price:
            change = (initial - price) / initial * 100
            line += (
                _(" <i>(-{pct:.1f}%)</i>") if change > 0 else _(" <i>(+{pct:.1f}%)</i>")
            ).format(pct=abs(change))
        lowest = _safe_dec(product.get("lowest_price"))
        if lowest and lowest < price:
            line += _("  min €{price:.2f}").format(price=lowest)
        lines.append(line)

    unpriced = [p for p, price in priced if price is None]
    for product in unpriced:
        name = _escape_html(product_label(product))
        lines.append(_("<b>#{pid}</b> {name} — no price yet").format(pid=product["id"], name=name))

    if len(known) >= 2:
        cheapest = min(known, key=lambda row: row[1])
        dearest = max(known, key=lambda row: row[1])
        lines += [
            "",
            _("🥇 Cheapest now: <b>{name}</b> at €{price:.2f}").format(
                name=_escape_html(product_label(cheapest[0])),
                price=cheapest[1],
            ),
            _("↔️ Spread: €{spread:.2f}").format(spread=dearest[1] - cheapest[1]),
        ]
    return "\n".join(lines)


# ── The panels ───────────────────────────────────────────────────────────


def build_groups_list(
    groups: Sequence[Any],
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """The user's groups, one button each."""
    exits = (nav_row(context, message_id) if context is not None else [close_button()]) or [
        close_button()
    ]
    rows = [
        [
            InlineKeyboardButton(
                _("🏷 {name} ({count})").format(
                    name=truncate_visible(group.name, GROUP_NAME_BUDGET),
                    count=group.member_count,
                ),
                callback_data=f"{GROUP_OPEN_PREFIX}{group.id}",
            )
        ]
        for group in groups
    ]
    rows.append([InlineKeyboardButton(_("➕ New group"), callback_data="grp_new")])
    rows.append(exits)

    if not groups:
        text = _(
            "🏷 <b>Groups</b>\n\n"
            "A group is a set of products you want to compare — three monitors, "
            "say, or everything on one shopping list.\n\n"
            "You have none yet."
        )
    else:
        text = _("🏷 <b>Groups</b> ({count})\n\nPick one to compare its products.").format(
            count=len(groups)
        )
    return text, InlineKeyboardMarkup(rows)


def build_group_view(
    group: Any,
    products: Sequence[dict[str, Any]],
    *,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    message_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """One group: what is in it, and what can be done with it."""
    exits = (nav_row(context, message_id) if context is not None else [close_button()]) or [
        close_button()
    ]
    header = _("🏷 <b>{name}</b> — {count} products").format(
        name=_escape_html(truncate_visible(group.name, GROUP_NAME_BUDGET)),
        count=len(products),
    )
    if not products:
        text = "\n\n".join([header, _("Nothing in it yet. Add products to start comparing them.")])
    else:
        lines = [header, ""]
        for product in products:
            price = _safe_dec(product.get("current_price"))
            name = _escape_html(product_label(product))
            price_str = f"€{price:.2f}" if price else _("N/A")
            lines.append(f"  <b>#{product['id']}</b> {name} — {price_str}")
        text = "\n".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    if len(products) >= 2:
        rows.append(
            [
                InlineKeyboardButton(_("📊 Compare"), callback_data=f"grp_cmp_{group.id}"),
                InlineKeyboardButton(_("📈 Chart"), callback_data=f"grp_chart_{group.id}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    _("🕐 Who has been cheapest"), callback_data=f"grp_lead_{group.id}"
                )
            ]
        )
    add_row = [InlineKeyboardButton(_("➕ Add product"), callback_data=f"grp_add_{group.id}")]
    if products:
        add_row.append(InlineKeyboardButton(_("➖ Remove"), callback_data=f"grp_rem_{group.id}"))
    rows.append(add_row)
    rows.append([InlineKeyboardButton(_("✏️ Rename"), callback_data=f"grp_ren_{group.id}")])
    rows.append([InlineKeyboardButton(_("🗑 Delete group"), callback_data=f"grp_del_{group.id}")])
    rows.append(exits)
    return text, InlineKeyboardMarkup(rows)
