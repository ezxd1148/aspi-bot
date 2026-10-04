"""aspi-bot — polls Tally, sends submissions for admin review, then broadcasts."""

import asyncio
import os
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.error import TelegramError

import lib.config

# Load this checkout's configuration before modules compute their data paths.
try:
    lib.config.load_environment()
    REVIEW_CHAT_ID, REVIEW_CONFIG_KEY = lib.config.review_destination()
except ValueError as error:
    raise SystemExit(f"Configuration error: {error}") from error

import lib.fetch_form
import lib.moderation
import lib.pending
import lib.tally_admin
import lib.tracker
import lib.web_server

# ── Environment ───────────────────────────────────────────────────────────────

TALLY_API_KEY = os.getenv("TALLY_API_KEY")
FORM_ID = os.getenv("FORM_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
RESET_HOUR = int(os.getenv("RESET_HOUR", "3"))
if POLL_INTERVAL <= 0 or not 0 <= RESET_HOUR <= 23:
    raise SystemExit("POLL_INTERVAL_SECONDS must be positive and RESET_HOUR must be 0-23.")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_ID = hashlib.sha256(b"".join(
    (PROJECT_ROOT / name).read_bytes() for name in (
        "src/bot.py", "src/lib/config.py", "src/lib/moderation.py",
        "src/lib/Prompt/SYSTEM_PROMPT.md",
    )
)).hexdigest()[:12]

REQUIRED_VARS = {
    "TALLY_API_KEY": TALLY_API_KEY,
    "FORM_ID": FORM_ID,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
    REVIEW_CONFIG_KEY: REVIEW_CHAT_ID,
}
missing = [name for name, val in REQUIRED_VARS.items() if not val or not val.strip()]
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
    if chat is None or user is None or str(chat.id) != REVIEW_CHAT_ID or user.is_bot:
        return False
    if chat.type == "private":
        return str(user.id) == REVIEW_CHAT_ID
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
        "<code>ADMIN_GROUP_ID</code> to that group's chat ID in <b>.env</b>. "
        "The group owner and administrators can approve, reject, /testbots, and /reset. "
        "Make the bot a group administrator, then restart it after editing .env.\n\n"
        f"Loaded review destination: <code>{REVIEW_CHAT_ID}</code> "
        f"via <code>{REVIEW_CONFIG_KEY}</code>.\nBuild: <code>{BUILD_ID}</code>\n"
        "Use /help for commands and /status in the configured review chat.",
        parse_mode="HTML",
    )


async def help_command(update: Update, context) -> None:
    await update.message.reply_text(
        "Commands:\n/start - chat ID and loaded review destination\n"
        "/help - this command list\n/status - review group and bot permission checks\n"
        "/testbots - test moderation providers\n/reset - delete Tally submissions and local review state\n\n"
        "Status, tests, reset, and review buttons require an administrator in the configured review chat. "
        "In a group, use /command@YourBotUsername."
    )


async def _validate_review_destination(bot):
    """Fail before polling if the bot cannot verify the configured destination."""
    chat = await bot.get_chat(REVIEW_CHAT_ID)
    if str(chat.id) != REVIEW_CHAT_ID:
        raise RuntimeError("Telegram returned a different review chat ID; update .env with /start's ID.")
    if int(REVIEW_CHAT_ID) < 0:
        if chat.type not in ("group", "supergroup"):
            raise RuntimeError("The review destination must be a group or supergroup.")
        if chat.username:
            raise RuntimeError("The review group is public. Configure a private admin group.")
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
        if member.status not in ("creator", "administrator"):
            raise RuntimeError("Promote the bot to administrator in the private review group.")
    elif chat.type != "private":
        raise RuntimeError("Legacy positive ADMIN_CHAT_ID must identify a private chat.")
    return chat


async def status_command(update: Update, context) -> None:
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Admin only. Run /start to see the loaded review destination.")
        return
    try:
        chat = await _validate_review_destination(context.bot)
        permission = "PASS - review destination and bot membership verified"
        title = chat.title or "Private admin chat (legacy mode)"
    except (TelegramError, RuntimeError) as error:
        permission = f"FAIL - {error}" if isinstance(error, RuntimeError) else f"FAIL - Telegram {type(error).__name__}"
        title = "Review destination could not be verified"
    await update.message.reply_text(
        f"Bot status\nBuild: {BUILD_ID}\nProject: {PROJECT_ROOT}\nConfig file: {lib.config.ENV_FILE}\n"
        f"Review chat: {REVIEW_CHAT_ID} via {REVIEW_CONFIG_KEY}\n{title}\n{permission}\n"
        f"Channel: {TELEGRAM_CHANNEL_ID}\n"
        f"Commands: {', '.join('/' + name for name, *_ in COMMANDS)}",
        parse_mode=None,
    )


async def unknown_command(update: Update, context) -> None:
    command = (update.message.text or "").split()[0]
    if "@" in command and command.split("@", 1)[1].casefold() != context.bot.username.casefold():
        return
    await update.message.reply_text("Unknown command. Use /help to see the available commands.")


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
        ], return_exceptions=True)
        lines = ["Moderation provider test:"]
        for api, result in zip(lib.moderation.APIS, results):
            if isinstance(result, BaseException):
                result = f"FAIL - test error ({type(result).__name__})"
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
        try:
            await loop.run_in_executor(
                None, lib.tally_admin.delete_all_submissions, TALLY_API_KEY, FORM_ID
            )
        except Exception as error:
            await update.message.reply_text(
                f"❌ Tally reset failed ({type(error).__name__}). Local pending reviews were preserved."
            )
            return
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
                chat_id=REVIEW_CHAT_ID,
                text=dm_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            # Make the entry available before a reviewer can act on the buttons.
            lib.pending.save_pending(sid, text, files)
            lib.tracker.mark_processed(sid)
        await _send_files(context.bot, REVIEW_CHAT_ID, files, reply_to=msg.message_id)
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
                    chat_id=REVIEW_CHAT_ID,
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
        try:
            await loop.run_in_executor(
                None, lib.tally_admin.delete_all_submissions, TALLY_API_KEY, FORM_ID
            )
        except Exception as error:
            print(f"Daily reset failed ({type(error).__name__}); local review state preserved.", flush=True)
            return
        lib.tracker.reset()
        lib.pending.clear_all()

    print("=== Daily reset complete ===")


# ── Main ──────────────────────────────────────────────────────────────────────


async def _error_handler(update: object, context) -> None:
    """Log errors cleanly — suppresses noisy tracebacks for transient network issues."""
    err = context.error
    print(f"Handler error: {type(err).__name__}", flush=True)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "The request failed. Check the bot service logs and retry."
            )
        except TelegramError:
            pass


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


# One registry owns both dispatch and the command menu.
COMMANDS = (
    ("start", start_command, "Show chat ID and review destination", False),
    ("help", help_command, "List available commands", False),
    ("status", status_command, "Check review group configuration", True),
    ("testbots", testbots_command, "Test moderation providers", True),
    ("reset", reset_command, "Delete Tally submissions and review state", True),
)


async def _post_init(app) -> None:
    # Validate first: a broken group configuration must not silently send to a DM.
    await _validate_review_destination(app.bot)
    print(
        f"Build {BUILD_ID}; project={PROJECT_ROOT}; config={lib.config.ENV_FILE}; "
        f"review={REVIEW_CHAT_ID} via {REVIEW_CONFIG_KEY}; channel={TELEGRAM_CHANNEL_ID}",
        flush=True,
    )
    if int(REVIEW_CHAT_ID) > 0:
        print("Legacy private-admin mode. Set ADMIN_GROUP_ID to enable shared review.", flush=True)
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, _, description, admin in COMMANDS if not admin],
            scope=BotCommandScopeDefault(),
        )
        scope = (BotCommandScopeChatAdministrators(chat_id=int(REVIEW_CHAT_ID))
                 if int(REVIEW_CHAT_ID) < 0 else BotCommandScopeChat(chat_id=int(REVIEW_CHAT_ID)))
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, _, description, _ in COMMANDS], scope=scope
        )
    except TelegramError as error:
        print(f"Command menu update failed ({type(error).__name__}); typed commands remain registered.", flush=True)
    await asyncio.get_running_loop().run_in_executor(None, _seed_tracker)
    lib.web_server.start()


def create_application():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .post_init(_post_init)
        .build()
    )

    for name, handler, _, _ in COMMANDS:
        app.add_handler(CommandHandler(name, handler, block=name != "testbots"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_error_handler(_error_handler)
    return app


def main() -> None:
    app = create_application()
    app.job_queue.run_repeating(check_tally, interval=POLL_INTERVAL, first=5)

    # Daily reset: schedule at a fixed hour (default 03:00), not relative to startup
    reset_hour = RESET_HOUR
    now = datetime.now()
    reset_time = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    if reset_time <= now:
        reset_time += timedelta(days=1)
    first_delay = (reset_time - now).total_seconds()
    app.job_queue.run_repeating(daily_reset, interval=86400, first=first_delay)
    print(
        f"Daily reset scheduled at {reset_hour:02d}:00 (in {first_delay / 3600:.1f}h)."
    )

    print(f"Bot starting. Polling Tally every {POLL_INTERVAL}s.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-config"]:
        print(f"Project: {PROJECT_ROOT}\nConfig: {lib.config.ENV_FILE}\nBuild: {BUILD_ID}\n"
              f"Review destination: {REVIEW_CHAT_ID} via {REVIEW_CONFIG_KEY}\nChannel: {TELEGRAM_CHANNEL_ID}")
    else:
        main()
