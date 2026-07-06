"""aspi-bot — polls Tally, DMs admin for manual review, then broadcasts."""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

import lib.fetch_form
import lib.pending
import lib.tracker

# ── Environment ───────────────────────────────────────────────────────────────

load_dotenv()

TALLY_API_KEY = os.getenv("TALLY_API_KEY")
FORM_ID = os.getenv("FORM_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

REQUIRED_VARS = {
    "TALLY_API_KEY": TALLY_API_KEY,
    "FORM_ID": FORM_ID,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
    "ADMIN_CHAT_ID": ADMIN_CHAT_ID,
}
missing = [name for name, val in REQUIRED_VARS.items() if val is None]
if missing:
    print(f"Missing environment variables: {', '.join(missing)}")
    sys.exit(1)


# ── Handlers ──────────────────────────────────────────────────────────────────


async def start_command(update: Update, context) -> None:
    """Show the user their chat ID (for .env setup)."""
    await update.message.reply_text(
        f"👋 Your chat ID is: <code>{update.effective_chat.id}</code>\n\n"
        "Set <code>ADMIN_CHAT_ID</code> to this value in your <b>.env</b> file.",
        parse_mode="HTML",
    )


async def check_tally(context) -> None:
    """JobQueue callback: poll Tally for new submissions, DM admin."""
    loop = asyncio.get_running_loop()

    # fetch_data is blocking (uses requests), so run in thread
    data = await loop.run_in_executor(
        None, lib.fetch_form.fetch_data, TALLY_API_KEY, FORM_ID
    )
    if data is None:
        print("Tally fetch failed.")
        return

    submissions = data.get("submissions", [])
    for sub in submissions:
        sid = sub.get("id")
        if not sid or lib.tracker.is_processed(sid):
            continue

        answer = lib.fetch_form.get_answers(sub)
        if answer == "No text found.":
            lib.tracker.mark_processed(sid)
            continue

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"ok_{sid}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"no_{sid}"),
                ]
            ]
        )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"📩 <b>New confession:</b>\n\n{answer}",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            lib.tracker.mark_processed(sid)
            lib.pending.save_pending(sid, answer)
            print(f"Notified admin: {answer[:60]}...")
        except Exception as e:
            print(f"Failed to DM admin: {e}")
            # Don't mark as processed — will retry next poll


async def button_handler(update: Update, context) -> None:
    """Handle Approve / Reject inline button clicks."""
    query = update.callback_query
    await query.answer()

    action, sid = query.data.split("_", 1)

    text = lib.pending.get_pending(sid)
    if text is None:
        await query.edit_message_text("⚠️ Submission no longer available.")
        return

    if action == "ok":
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=text,
            )
            await query.edit_message_text(
                f"✅ <b>Approved &amp; sent:</b>\n\n{text}",
                parse_mode="HTML",
            )
            print(f"Approved: {text[:60]}...")
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to send to channel: {e}")
            return
    else:
        await query.edit_message_text(
            f"❌ <b>Rejected:</b>\n\n{text}",
            parse_mode="HTML",
        )
        print(f"Rejected: {text[:60]}...")

    lib.pending.remove_pending(sid)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(check_tally, interval=POLL_INTERVAL, first=5)

    print(f"Bot running. Polling Tally every {POLL_INTERVAL}s.")
    print(f"Admin chat: {ADMIN_CHAT_ID}, Channel: {TELEGRAM_CHANNEL_ID}")
    app.run_polling()


if __name__ == "__main__":
    main()
