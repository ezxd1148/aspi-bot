"""aspi-bot — polls Tally, DMs admin for manual review, then broadcasts."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

import lib.fetch_form
import lib.moderation
import lib.pending
import lib.tally_admin
import lib.tracker
import lib.web_server

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

MAX_MEDIA_PER_GROUP = 10
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _split_files(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split file list into (images, documents) based on mime type."""
    images, docs = [], []
    for f in files:
        if f.get("mime_type", "") in IMAGE_MIME_TYPES:
            images.append(f)
        else:
            docs.append(f)
    return images, docs


async def _send_files(
    bot, chat_id: str, files: list[dict], reply_to: int | None = None
) -> None:
    """Send file attachments below the DM — photos render inline, docs as files."""
    if not files:
        return

    images, docs = _split_files(files)

    # Photos: InputMediaPhoto renders as full inline images
    for i in range(0, len(images), MAX_MEDIA_PER_GROUP):
        chunk = images[i : i + MAX_MEDIA_PER_GROUP]
        media = [InputMediaPhoto(media=f["url"]) for f in chunk]
        try:
            await bot.send_media_group(
                chat_id=chat_id, media=media, reply_to_message_id=reply_to
            )
        except Exception as e:
            print(f"Failed to send images: {e}")

    # Docs: InputMediaDocument for non-image files
    for i in range(0, len(docs), MAX_MEDIA_PER_GROUP):
        chunk = docs[i : i + MAX_MEDIA_PER_GROUP]
        media = [InputMediaDocument(media=f["url"]) for f in chunk]
        if len(chunk) == 1:
            media[0].caption = chunk[0]["name"]
        try:
            await bot.send_media_group(
                chat_id=chat_id, media=media, reply_to_message_id=reply_to
            )
        except Exception as e:
            print(f"Failed to send docs: {e}")


async def _broadcast(bot, text: str, files: list[dict]) -> None:
    """Send confession to channel, combining text and files into one post."""
    images, docs = _split_files(files)

    sent_anything = False

    # Photos: send as photo group with confession text as caption
    if images:
        for i in range(0, len(images), MAX_MEDIA_PER_GROUP):
            chunk = images[i : i + MAX_MEDIA_PER_GROUP]
            media = []
            for j, f in enumerate(chunk):
                cap = (
                    text if (i == 0 and j == 0 and not sent_anything and text) else None
                )
                media.append(InputMediaPhoto(media=f["url"], caption=cap))
            try:
                await bot.send_media_group(chat_id=TELEGRAM_CHANNEL_ID, media=media)
                sent_anything = True
            except Exception as e:
                print(f"Failed to broadcast images: {e}")

    # Docs: send text first (if not already captioned), then files
    if docs:
        if text and not sent_anything:
            await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text)
            sent_anything = True
        for i in range(0, len(docs), MAX_MEDIA_PER_GROUP):
            chunk = docs[i : i + MAX_MEDIA_PER_GROUP]
            media = [InputMediaDocument(media=f["url"]) for f in chunk]
            if len(chunk) == 1:
                media[0].caption = chunk[0]["name"]
            try:
                await bot.send_media_group(chat_id=TELEGRAM_CHANNEL_ID, media=media)
            except Exception as e:
                print(f"Failed to broadcast docs: {e}")

    # Text-only: plain message
    if text and not sent_anything:
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text)


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

    data = await loop.run_in_executor(
        None, lib.fetch_form.fetch_data, TALLY_API_KEY, FORM_ID
    )
    if data is None:
        print("Tally fetch failed.")
        return

    submissions = data.get("submissions", [])
    tasks = []

    for sub in submissions:
        sid = sub.get("id")
        if not sid or lib.tracker.is_processed(sid):
            continue

        text, files = lib.fetch_form.extract_submission(sub)
        if not text and not files:
            lib.tracker.mark_processed(sid)
            continue

        tasks.append(_handle_submission(context, sid, text, files))

    if tasks:
        await asyncio.gather(*tasks)


async def _handle_submission(context, sid: str, text: str, files: list[dict]) -> None:
    """Process a single submission: moderate, then auto-broadcast or DM admin."""
    loop = asyncio.get_running_loop()

    # ── AI moderation ──
    if text:
        result = await loop.run_in_executor(None, lib.moderation.moderate_text, text)
        if result == "clean":
            await _broadcast(context.bot, text, files)
            lib.tracker.mark_processed(sid)
            print(f"  Auto-approved: {text[:60]}...")
            return

    # ── Manual review (flagged or files-only) ──
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"ok_{sid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"no_{sid}"),
            ]
        ]
    )

    dm_text = (
        f"📩 <b>New confession:</b>\n\n{text}"
        if text
        else "📩 <b>New confession (files only):</b>"
    )
    if files:
        names = "\n".join(f"📎 {f['name']}" for f in files)
        dm_text += f"\n\n<b>Attachments:</b>\n{names}"

    try:
        msg = await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=dm_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await _send_files(context.bot, ADMIN_CHAT_ID, files, reply_to=msg.message_id)

        lib.tracker.mark_processed(sid)
        lib.pending.save_pending(sid, text, files)
        print(f"Notified admin: {text[:60] if text else '(files only)'}...")
    except Exception as e:
        print(f"Failed to DM admin: {e}")


async def button_handler(update: Update, context) -> None:
    """Handle Approve / Reject inline button clicks."""
    query = update.callback_query
    await query.answer()

    action, sid = query.data.split("_", 1)

    pending = lib.pending.get_pending(sid)
    if pending is None:
        await query.edit_message_text("⚠️ Submission no longer available.")
        return

    text = pending["text"]
    files = pending["files"]

    if action == "ok":
        try:
            await _broadcast(context.bot, text, files)

            label = "✅ <b>Approved &amp; sent</b>"
            if text:
                label += f":\n\n{text}"
            await query.edit_message_text(label, parse_mode="HTML")
            print(f"Approved: {text[:60] if text else '(files only)'}...")
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to send to channel: {e}")
            return
    else:
        label = "❌ <b>Rejected</b>"
        if text:
            label += f":\n\n{text}"
        await query.edit_message_text(label, parse_mode="HTML")
        print(f"Rejected: {text[:60] if text else '(files only)'}...")

    lib.pending.remove_pending(sid)


async def daily_reset(context) -> None:
    """Reset Tally submissions and local state every 24 hours."""
    print("=== Daily reset starting ===")
    loop = asyncio.get_running_loop()

    # Clear Tally submissions
    await loop.run_in_executor(
        None, lib.tally_admin.delete_all_submissions, TALLY_API_KEY, FORM_ID
    )

    # Clear local state
    lib.tracker.reset()
    lib.pending.clear_all()

    print("=== Daily reset complete ===")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(check_tally, interval=POLL_INTERVAL, first=5)

    # Daily reset: schedule at a fixed hour (default 03:00), not relative to startup
    reset_hour = int(os.getenv("RESET_HOUR", "3"))
    now = datetime.now()
    reset_time = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if reset_time <= now:
        reset_time += timedelta(days=1)
    first_delay = (reset_time - now).total_seconds()
    app.job_queue.run_repeating(daily_reset, interval=86400, first=first_delay)
    print(
        f"Daily reset scheduled at {reset_hour:02d}:00 (in {first_delay / 3600:.1f}h)."
    )

    lib.web_server.start()

    print(f"Bot running. Polling Tally every {POLL_INTERVAL}s.")
    print(f"Admin chat: {ADMIN_CHAT_ID}, Channel: {TELEGRAM_CHANNEL_ID}")
    app.run_polling()


if __name__ == "__main__":
    main()
