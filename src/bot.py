"""aspi-bot — polls Tally, sends submissions for admin review, then broadcasts."""

import asyncio
import os
import sys
import hashlib
import time
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
from telegram.error import Conflict, TelegramError

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
        "src/lib/dashboard.py", "src/lib/dashboard_backend.py", "src/lib/dashboard_store.py",
        "src/lib/pending.py", "src/requirements.txt",
        "src/dashboard/index.html", "src/dashboard/login.html", "src/dashboard/app.js", "src/dashboard/app.css",
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


def _runtime(context) -> dict:
    return context.bot_data.setdefault("runtime", {})


def _event(context, kind: str, **metadata) -> None:
    store = context.bot_data.get("audit_store")
    if store is not None:
        try:
            store.record(kind, **metadata)
        except Exception as error:
            _runtime(context)["audit_error"] = type(error).__name__
            print(f"Audit log write failed: {type(error).__name__}", flush=True)


async def _probe_providers(context) -> list[dict]:
    """Both interfaces use the same provider probe and safe result formatting."""
    runtime = _runtime(context)
    runtime["provider_test_started"] = time.time()
    _event(context, "provider_test_started")
    results = await asyncio.gather(*[
        asyncio.get_running_loop().run_in_executor(None, lib.moderation.test_provider, api)
        for api in lib.moderation.APIS
    ], return_exceptions=True)
    formatted = [{"name": api["name"], "model": api["model"],
                  "result": f"FAIL - test error ({type(result).__name__})" if isinstance(result, BaseException) else result}
                 for api, result in zip(lib.moderation.APIS, results)]
    runtime["provider_results"] = formatted
    runtime["provider_test_finished"] = time.time()
    _event(context, "provider_test_complete")
    return formatted


async def _decide_submission(context, sid: str, action: str, user, source="telegram") -> dict:
    """One decision/publishing path and lock for Telegram and the dashboard."""
    if action not in ("ok", "no"):
        raise ValueError("Invalid review action")
    async with _review_lock(context):
        pending = lib.pending.get_pending(sid)
        if pending is None:
            return {"status": "missing"}
        if action == "ok":
            try:
                await _broadcast(context.bot, pending["text"], pending["files"])
            except Exception as error:
                _event(context, "publish_failed", submission_id=sid, actor=user, source=source,
                       detail=type(error).__name__)
                print(f"Failed to publish approved submission: {type(error).__name__}", flush=True)
                return {"status": "failed"}
        lib.pending.remove_pending(sid)
        _event(context, "approved" if action == "ok" else "rejected",
               submission_id=sid, actor=user, source=source)
        print(f"Review {action} by user {user.id}: {sid}", flush=True)
        return {"status": "done", "pending": pending}


def _decision_label(pending, action, user):
    label = ("✅ <b>Approved &amp; sent</b>" if action == "ok" else "❌ <b>Rejected</b>")
    label += f" by {escape(user.full_name)}"
    if pending["text"]:
        label += f":\n\n{escape(pending['text'])}"
    return label


async def _reset_submissions(context, *, actor=None, source="telegram", scheduled=False) -> dict:
    runtime = _runtime(context)
    if runtime.get("reset_running"):
        return {"ok": False, "phase": "busy"}
    runtime["reset_running"] = True
    started = time.time()
    runtime["last_reset"] = {"started": started, "status": "running", "source": source}
    _event(context, "reset_started", actor=actor, source=source)
    try:
        async with _review_lock(context):
            if scheduled:
                await _notify_daily_reset(context, "🔄 Daily reset starting: clearing Tally submissions and local review state.")
            elif source == "dashboard":
                await _notify_daily_reset(context, f"🔄 Dashboard reset starting, requested by {actor.full_name}.")
            try:
                deleted = await asyncio.get_running_loop().run_in_executor(
                    None, lib.tally_admin.delete_all_submissions, TALLY_API_KEY, FORM_ID)
            except Exception as error:
                result = {"ok": False, "phase": "tally", "error": type(error).__name__}
            else:
                try:
                    lib.tracker.reset()
                    lib.pending.clear_all()
                except Exception as error:
                    result = {"ok": False, "phase": "local", "error": type(error).__name__, "deleted": deleted}
                else:
                    result = {"ok": True, "deleted": deleted}
        runtime["last_reset"] = {"started": started, "finished": time.time(), "source": source,
                                 "status": "complete" if result["ok"] else "failed", **result}
        _event(context, "reset_complete" if result["ok"] else "reset_failed", actor=actor, source=source,
               detail=f"Deleted {result['deleted']} Tally submissions" if result["ok"] else f"{result['phase']}: {result['error']}")
        return result
    finally:
        runtime["reset_running"] = False


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


def _admin_identity(user) -> str:
    name = " ".join(user.full_name.split())
    username = f" @{user.username}" if user.username else ""
    return f"{name}{username} (ID: {user.id})"


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
    requester = update.effective_user
    requester_role = "Private admin" if update.effective_chat.type == "private" else "Group admin (verified)"
    admin_lines = []
    if update.effective_chat.type == "private":
        admin_lines = ["Detected admins (1):", f"- {_admin_identity(requester)} - Private admin (you)"]
    else:
        try:
            members = await context.bot.get_chat_administrators(chat_id=int(REVIEW_CHAT_ID))
            admins = sorted(
                (member for member in members if not member.user.is_bot
                 and member.status in ("creator", "administrator")),
                key=lambda member: (member.status != "creator", member.user.full_name.casefold(), member.user.id),
            )
            admin_lines.append(f"Detected admins ({len(admins)}):")
            for member in admins:
                role = "Owner" if member.status == "creator" else "Administrator"
                marker = " (you)" if member.user.id == requester.id else ""
                if marker:
                    requester_role = role
                admin_lines.append(f"- {_admin_identity(member.user)} - {role}{marker}")
            if not admins:
                admin_lines.append("No human admins returned by Telegram.")
        except TelegramError as error:
            admin_lines.append(f"Detected admins: lookup failed (Telegram {type(error).__name__})")
    report = (
        f"Bot status\nBuild: {BUILD_ID}\nProject: {PROJECT_ROOT}\nConfig file: {lib.config.ENV_FILE}\n"
        f"Review chat: {REVIEW_CHAT_ID} via {REVIEW_CONFIG_KEY}\n{title}\n{permission}\n"
        f"Channel: {TELEGRAM_CHANNEL_ID}\n"
        f"Commands: {', '.join('/' + name for name, *_ in COMMANDS)}\n"
        f"Requested by: {_admin_identity(requester)} - {requester_role}\n"
        + "\n".join(admin_lines)
    )
    # Large admin groups can exceed Telegram's message limit; keep every admin visible.
    page = ""
    for line in report.splitlines():
        if len(page) + len(line) + 1 > 3500:
            await update.message.reply_text(page, parse_mode=None)
            page = ""
        page += ("\n" if page else "") + line
    if page:
        await update.message.reply_text(page, parse_mode=None)


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
    if time.time() - _runtime(context).get("provider_test_started", 0) < 60:
        await update.message.reply_text("Wait a minute between provider tests.")
        return

    context.bot_data["provider_test_running"] = True
    try:
        await update.message.reply_text(
            "Testing each moderation provider with CLEAN and FLAGGED samples. "
            "This can take about two minutes and uses API quota/credits."
        )
        results = await _probe_providers(context)
        lines = ["Moderation provider test:"]
        for result in results:
            lines.append(f"\n{result['name']} ({result['model']})\n{result['result']}")
        await update.message.reply_text("\n".join(lines), parse_mode=None)
    finally:
        context.bot_data.pop("provider_test_running", None)


async def reset_command(update: Update, context) -> None:
    """Admin-only: clear all local state and Tally submissions."""
    if not await _is_admin(update, context):
        await update.message.reply_text("⛔ Admin only.")
        return

    await update.message.reply_text("🔄 Clearing Tally submissions...")
    result = await _reset_submissions(context, actor=update.effective_user)
    if result["ok"]:
        message = "✅ All cleared — tracker, pending, and Tally."
    elif result["phase"] == "busy":
        message = "A reset is already running."
    elif result["phase"] == "tally":
        message = f"❌ Tally reset failed ({result['error']}). Local pending reviews were preserved."
    else:
        message = f"❌ Local reset failed ({result['error']}). Tally was cleared; check the service logs."
    await update.message.reply_text(message)


async def check_tally(context) -> None:
    """JobQueue callback: poll Tally for new submissions, notify the review chat."""
    loop = asyncio.get_running_loop()

    runtime = _runtime(context)
    runtime["last_poll_attempt"] = time.time()
    try:
        data = await loop.run_in_executor(
            None, lib.fetch_form.fetch_data, TALLY_API_KEY, FORM_ID
        )
    except Exception as error:
        runtime["last_poll_error"] = type(error).__name__
        _event(context, "poll_failed", detail=type(error).__name__)
        print(f"Tally fetch failed: {type(error).__name__}", flush=True)
        return
    if data is None:
        runtime["last_poll_error"] = "Tally request failed"
        _event(context, "poll_failed", detail="Tally request failed")
        print("Tally fetch failed.")
        return
    runtime["last_poll_success"] = time.time()
    runtime["last_poll_error"] = None

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
    moderation_result = "files_only"
    if text:
        moderation = await loop.run_in_executor(None, lib.moderation.moderate_submission, text)
        review_reason = moderation["reason"]
        moderation_result = moderation["result"]
        if moderation_result == "error":
            _event(context, "moderation_failed", submission_id=sid, detail="Providers unavailable; manual review required")
        if moderation["result"] == "clean":
            async with _review_lock(context):
                await _broadcast(context.bot, text, files)
                lib.tracker.mark_processed(sid)
                _event(context, "auto_approved", submission_id=sid)
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
            lib.pending.update_pending(sid, reason=review_reason, moderation=moderation_result,
                                       created_at=time.time(), message_id=msg.message_id)
            lib.tracker.mark_processed(sid)
            _event(context, "pending", submission_id=sid, detail=moderation_result)
        await _send_files(context.bot, REVIEW_CHAT_ID, files, reply_to=msg.message_id)
        print(f"Notified review chat: {text[:60] if text else '(files only)'}...")
    except Exception as e:
        _event(context, "review_delivery_failed", submission_id=sid, detail=type(e).__name__)
        print(f"Failed to notify review chat: {type(e).__name__}")


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

    result = await _decide_submission(context, sid, action, update.effective_user)
    if result["status"] == "missing":
        return
    if result["status"] == "failed":
        await context.bot.send_message(
            chat_id=REVIEW_CHAT_ID,
            text="❌ Publishing failed. The submission remains pending; try again after checking the channel.",
        )
        return
    try:
        await query.edit_message_text(_decision_label(result["pending"], action, update.effective_user),
                                      parse_mode="HTML", reply_markup=None)
    except TelegramError as error:
        print(f"Decision saved; review message edit failed: {type(error).__name__}", flush=True)


async def _notify_daily_reset(context, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=REVIEW_CHAT_ID, text=text, parse_mode=None)
    except TelegramError as error:
        print(f"Daily reset notification failed ({type(error).__name__}).", flush=True)


async def daily_reset(context) -> None:
    """Reset Tally submissions and local state every 24 hours."""
    print("=== Daily reset starting ===")
    result = await _reset_submissions(context, source="schedule", scheduled=True)
    if "next_reset" in _runtime(context):
        _runtime(context)["next_reset"] += 86400
    if result["ok"]:
        print("=== Daily reset complete ===")
        message = "✅ Daily reset complete: Tally submissions, tracker, and pending reviews cleared."
    elif result["phase"] == "busy":
        message = "Daily reset skipped: another reset is already running."
    elif result["phase"] == "tally":
        message = f"❌ Daily Tally reset failed ({result['error']}). Local pending reviews were preserved; check the service logs."
    else:
        message = f"❌ Daily reset failed while clearing local review state ({result['error']}). Tally was cleared; check the service logs."
    await _notify_daily_reset(context, message)


# ── Main ──────────────────────────────────────────────────────────────────────


async def _error_handler(update: object, context) -> None:
    """Log errors cleanly — suppresses noisy tracebacks for transient network issues."""
    err = context.error
    _event(context, "handler_failed", detail=type(err).__name__)
    print(f"Handler error: {type(err).__name__}", flush=True)
    if isinstance(err, Conflict):
        if "webhook" in str(err).lower():
            print(
                "Telegram polling/webhook conflict: another deployment is configuring a webhook. "
                "Stop that deployment before using this polling service.", flush=True,
            )
        else:
            print(
                "Telegram polling conflict: another process is using this bot token. "
                "Keep only aspi-bot.service running; check aspi-bot-ec2.service, "
                "manual Python sessions, local development, and other servers.", flush=True,
            )
        return
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
    from lib.dashboard import Dashboard, Settings
    from lib.dashboard_backend import BotBackend
    from lib.dashboard_store import AuditStore

    settings = Settings.from_environment()
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
    _runtime(app)["started"] = time.time()
    if settings:
        app.bot_data["audit_store"] = AuditStore(Path(lib.pending.PENDING_FILE).parent / "activity.sqlite3")
        dashboard = Dashboard(settings, app.bot, REVIEW_CHAT_ID, BotBackend(app, sys.modules[__name__]))
        app.bot_data["dashboard"] = dashboard
        await dashboard.start()
        _event(app, "bot_started", detail=f"Build {BUILD_ID}")
    lib.web_server.start()


async def _post_stop(app) -> None:
    dashboard = app.bot_data.get("dashboard")
    if dashboard:
        await dashboard.close()


def create_application():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .post_init(_post_init)
        .post_stop(_post_stop)
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
    _runtime(app)["next_reset"] = reset_time.astimezone().timestamp()
    _runtime(app)["reset_timezone"] = str(reset_time.astimezone().tzinfo)
    app.job_queue.run_repeating(daily_reset, interval=86400, first=first_delay)
    print(
        f"Daily reset scheduled at {reset_hour:02d}:00 (in {first_delay / 3600:.1f}h)."
    )

    print(f"Bot starting. Polling Tally every {POLL_INTERVAL}s.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-config"]:
        from lib.dashboard import Settings
        try:
            dashboard_settings = Settings.from_environment()
        except ValueError as error:
            raise SystemExit(f"Dashboard configuration error: {error}") from error
        print(f"Project: {PROJECT_ROOT}\nConfig: {lib.config.ENV_FILE}\nBuild: {BUILD_ID}\n"
              f"Review destination: {REVIEW_CHAT_ID} via {REVIEW_CONFIG_KEY}\nChannel: {TELEGRAM_CHANNEL_ID}")
        print(f"Dashboard: {dashboard_settings.origin} (127.0.0.1:{dashboard_settings.port})"
              if dashboard_settings else "Dashboard: disabled (DASHBOARD_URL is unset)")
    else:
        main()
