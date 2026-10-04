"""aspi-bot — polls Tally, sends submissions for admin review, then broadcasts."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler
from telegram.error import TelegramError

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


async def _is_admin(update: Update, context) -> bool:
    """Authorize a private admin, or the configured review group's real admins."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or str(chat.id) != ADMIN_CHAT_ID or user.is_bot:
        return False
    if chat.type == "private":
        return str(user.id) == ADMIN_CHAT_ID
    if chat.type not in ("group", "supergroup"):
        return False
    # Anonymous/channel-authored commands do not identify the requesting admin.
    if update.message and update.message.sender_chat is not None:
        return False
    try:
        member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
    except TelegramError as error:
        print(f"Could not verify admin membership: {type(error).__name__}", flush=True)
        return False
    return member.status in ("creator", "administrator")


def _review_lock(context) -> asyncio.Lock:
    """Serialize decisions and resets within the single running bot process."""
    return context.bot_data.setdefault("review_lock", asyncio.Lock())


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
                raise

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
                raise

    # Text-only: plain message
    if text and not sent_anything:
        await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text)


# ── Handlers ──────────────────────────────────────────────────────────────────


async def start_command(update: Update, context) -> None:
    """Show the user their chat ID (for .env setup)."""
    await update.message.reply_text(
        f"👋 Chat ID: <code>{update.effective_chat.id}</code>\n"
        f"Your user ID: <code>{update.effective_user.id}</code>\n\n"
        "For shared review, run /start in your private admin group and set "
        "<code>ADMIN_CHAT_ID</code> to that group's chat ID in <b>.env</b>. "
        "The group owner and administrators can approve, reject, /testbots, and /reset. "
        "Make the bot a group administrator, then restart it after editing .env.",
        parse_mode="HTML",
    )


async def testbots_command(update: Update, context) -> None:
    """Admin-only: test each moderation provider independently."""
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    if context.bot_data.get("provider_test_running"):
        await update.message.reply_text("A provider test is already running.")
        return

    context.bot_data["provider_test_running"] = True
    try:
        await update.message.reply_text(
            "Testing each moderation provider with CLEAN and FLAGGED samples. "
            "This can take about two minutes and uses API quota/credits."
        )
        loop = asyncio.get_running_loop()
        results = await asyncio.gather(*[
            loop.run_in_executor(None, lib.moderation.test_provider, api)
            for api in lib.moderation.APIS
        ])
        lines = ["Moderation provider test:"]
        for api, result in zip(lib.moderation.APIS, results):
            lines.append(f"\n{api['name']} ({api['model']})\n{result}")
        await update.message.reply_text("\n".join(lines), parse_mode=None)
    finally:
        context.bot_data.pop("provider_test_running", None)


async def reset_command(update: Update, context) -> None:
    """Admin-only: clear all local state and Tally submissions."""
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    loop = asyncio.get_running_loop()
    await update.message.reply_text("🔄 Clearing Tally submissions...")

    async with _review_lock(context):
        await loop.run_in_executor(
            None, lib.tally_admin.delete_all_submissions, TALLY_API_KEY, FORM_ID
        )
        lib.tracker.reset()
        lib.pending.clear_all()

    await update.message.reply_text("✅ All cleared — tracker, pending, and Tally.")


async def check_tally(context) -> None:
    """JobQueue callback: poll Tally for new submissions, notify the review chat."""
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
    """Process a submission: moderate, then auto-broadcast or notify the review chat."""
    loop = asyncio.get_running_loop()

    # ── AI moderation ──
    review_reason = ""
    if text:
        moderation = await loop.run_in_executor(None, lib.moderation.moderate_submission, text)
        review_reason = moderation["reason"]
        if moderation["result"] == "clean":
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
        f"📩 <b>New confession:</b>\n\n{escape(text)}"
        if text
        else "📩 <b>New confession (files only):</b>"
    )
    if files:
        names = "\n".join(f"📎 {escape(f['name'])}" for f in files)
        dm_text += f"\n\n<b>Attachments:</b>\n{names}"
    if review_reason:
        dm_text += f"\n\n<b>Review note:</b> {escape(review_reason)}"

    try:
        async with _review_lock(context):
            msg = await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=dm_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            # Make the entry available before a reviewer can act on the buttons.
            lib.pending.save_pending(sid, text, files)
            lib.tracker.mark_processed(sid)
        await _send_files(context.bot, ADMIN_CHAT_ID, files, reply_to=msg.message_id)
        print(f"Notified review chat: {text[:60] if text else '(files only)'}...")
    except Exception as e:
        print(f"Failed to notify review chat: {e}")


async def button_handler(update: Update, context) -> None:
    """Handle Approve / Reject inline button clicks."""
    query = update.callback_query
    if not await _is_admin(update, context):
        await query.answer("Only administrators of the configured review chat can review.", show_alert=True)
        return
    action, separator, sid = (query.data or "").partition("_")
    if not separator or not sid or action not in ("ok", "no"):
        await query.answer("Invalid review action.", show_alert=True)
        return
    await query.answer()

    async with _review_lock(context):
        pending = lib.pending.get_pending(sid)
        if pending is None:
            # Do not overwrite another admin's completed decision.
            return

        text = pending["text"]
        files = pending["files"]
        reviewer = escape(update.effective_user.full_name)
        if action == "ok":
            try:
                await _broadcast(context.bot, text, files)
            except Exception as e:
                print(f"Failed to publish approved submission: {type(e).__name__}", flush=True)
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text="❌ Publishing failed. The submission remains pending; try again after checking the channel.",
                )
                return
            label = f"✅ <b>Approved &amp; sent</b> by {reviewer}"
        else:
            label = f"❌ <b>Rejected</b> by {reviewer}"

        # Commit the decision before editing Telegram's display. A failed edit
        # must not allow another click to broadcast the same submission again.
        lib.pending.remove_pending(sid)
        if text:
            label += f":\n\n{escape(text)}"
        try:
            await query.edit_message_text(label, parse_mode="HTML", reply_markup=None)
        except TelegramError as error:
            print(f"Decision saved; review message edit failed: {type(error).__name__}", flush=True)
            return
        print(f"Review {action} by user {update.effective_user.id}: {sid}", flush=True)


async def daily_reset(context) -> None:
    """Reset Tally submissions and local state every 24 hours."""
    print("=== Daily reset starting ===")
    loop = asyncio.get_running_loop()

    # Clear Tally submissions
    async with _review_lock(context):
        await loop.run_in_executor(
            None, lib.tally_admin.delete_all_submissions, TALLY_API_KEY, FORM_ID
        )
        lib.tracker.reset()
        lib.pending.clear_all()

    print("=== Daily reset complete ===")


# ── Main ──────────────────────────────────────────────────────────────────────


async def _error_handler(update: object, context) -> None:
    """Log errors cleanly — suppresses noisy tracebacks for transient network issues."""
    err = context.error
    print(f"Non-critical error: {type(err).__name__}: {err}")


def _seed_tracker() -> None:
    """If the tracker is empty (e.g. data dir was wiped), pre-populate it
    with all existing Tally submission IDs so nothing gets re-sent."""
    if lib.tracker.load_processed():
        return  # already have state, nothing to do

    print("Tracker is empty — seeding with existing Tally submissions...")
    data = lib.fetch_form.fetch_data(TALLY_API_KEY, FORM_ID)
    if not data:
        print("  Could not fetch Tally data, will try again on first poll.")
        return

    count = 0
    for sub in data.get("submissions", []):
        sid = sub.get("id")
        if sid:
            lib.tracker.mark_processed(sid)
            count += 1

    print(f"  Seeded {count} existing submissions — nothing will re-send.")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("testbots", testbots_command, block=False))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(_error_handler)

    # ── Startup guard: seed tracker so vanished state doesn't cause re-sends ──
    _seed_tracker()

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
