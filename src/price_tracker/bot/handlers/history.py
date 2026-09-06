"""Price-history & reset handlers: /history, /reset.

Ported from monolithic bot.py [Task 17]. The chart renderer moved to
`bot.charts` once there was a second kind of chart to draw.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from price_tracker.bot.charts import generate_chart
from price_tracker.bot.decorators import _db, restricted, with_locale
from price_tracker.bot.handlers._helpers import (
    _escape_html,
    _get_user_product,
    _parse_id,
    _safe_dec,
)
from price_tracker.bot.keyboards import close_button
from price_tracker.bot.labels import product_label
from price_tracker.bot.messages import _

logger = logging.getLogger(__name__)


@with_locale
@restricted
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a price-history chart for a product."""
    if not context.args:
        # Show product picker
        db = _db(context)
        user_id = update.effective_user.id
        products = await db.get_active_products(user_id)
        if not products:
            await update.message.reply_text(_("📭 You have no tracked products."))
            return

        buttons = []
        for p in products:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"#{p['id']} {product_label(p, 35)}",
                        callback_data=f"chart_{p['id']}",
                    )
                ]
            )
        buttons.append([close_button()])

        await update.message.reply_text(
            _("📊 <b>Pick a product to see its history:</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    db = _db(context)
    chart_buf = await generate_chart(db, product_id, product)
    if chart_buf:
        lowest = _safe_dec(product.get("lowest_price"))
        highest = _safe_dec(product.get("highest_price"))
        # The image itself carries only "#id · shop": a caption has room for the
        # whole name, a line on a plot does not.
        caption = f"📊 <b>#{product_id}</b> {_escape_html(product_label(product, 80))}"
        if lowest:
            caption += _("\n📉 Min: €{price:.2f}").format(price=lowest)
        if highest:
            caption += _("  📈 Max: €{price:.2f}").format(price=highest)
        await update.message.reply_photo(
            photo=InputFile(chart_buf, filename=f"chart_{product_id}.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[close_button()]]),
        )
    else:
        await update.message.reply_text(
            _("📭 Not enough data to generate the chart (at least 2 points needed).")
        )


@with_locale
@restricted
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset initial_price to current_price for a product."""
    if not context.args:
        await update.message.reply_text(
            _(
                "❌ Usage: /reset &lt;id&gt;\n\n"
                "Resets the initial price to the current price.\n"
                "Useful when the price has dropped and you want to rebase the comparison."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    product_id = _parse_id(context.args[0])
    if product_id is None:
        await update.message.reply_text(_("❌ Invalid ID."))
        return

    product = await _get_user_product(context, product_id, update.effective_user.id)
    if not product:
        await update.message.reply_text(_("❌ Product not found."))
        return

    db = _db(context)
    success = await db.reset_initial_price(product_id)
    if success:
        name = (product.get("name") or _("Unknown"))[:60]
        current = _safe_dec(product.get("current_price"))
        price_str = f"€{current:.2f}" if current else _("N/A")
        await update.message.reply_text(
            _(
                "✅ Initial price updated!\n\n"
                "📦 <b>#{pid}</b> {name}\n"
                "💰 New base price: <b>{price}</b>"
            ).format(pid=product_id, name=_escape_html(name), price=price_str),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(_("❌ Could not update the initial price."))


def register(app: Application) -> None:
    """Register history/reset command handlers on `app`."""
    app.add_handler(CommandHandler("storia", cmd_history))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("azzera", cmd_reset))
