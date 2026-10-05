"""Dashboard views over the running bot; no second poller or duplicated decisions."""

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

from aiohttp import web
from telegram.error import TelegramError


class BotBackend:
    def __init__(self, app, bot_module):
        self.app = app
        self.module = bot_module
        self.context = SimpleNamespace(bot=app.bot, bot_data=app.bot_data)

    async def read(self, section, query):
        module = self.module
        runtime = module._runtime(self.context)
        store = self.app.bot_data["audit_store"]
        if section == "overview":
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            counts = store.counts(today)
            last_success = runtime.get("last_poll_success")
            disk = shutil.disk_usage(Path(module.lib.pending.PENDING_FILE).parent)
            last_reset = runtime.get("last_reset")
            if last_reset is None:
                previous = store.latest(("reset_started", "reset_complete", "reset_failed"))
                if previous:
                    last_reset = {"status": {"reset_started": "interrupted", "reset_complete": "complete",
                                              "reset_failed": "failed"}[previous["kind"]],
                                  "finished": previous["time"], "source": previous["source"],
                                  "detail": previous["detail"]}
            return {"pending": len(module.lib.pending.list_pending()),
                    "approved_today": counts.get("approved", 0) + counts.get("auto_approved", 0),
                    "rejected_today": counts.get("rejected", 0),
                    "errors_today": sum(count for kind, count in counts.items()
                                        if kind.endswith("failed") or kind.endswith("error")),
                    "stats_timezone": "UTC", "build": module.BUILD_ID,
                    "started": runtime.get("started"), "now": time.time(),
                    "last_poll_attempt": runtime.get("last_poll_attempt"), "last_poll_success": last_success,
                    "poll_healthy": bool(last_success and time.time() - last_success < module.POLL_INTERVAL * 2 + 30
                                         and not runtime.get("last_poll_error")),
                    "last_poll_error": runtime.get("last_poll_error"), "poll_interval": module.POLL_INTERVAL,
                    "review_chat_id": module.REVIEW_CHAT_ID, "channel_id": module.TELEGRAM_CHANNEL_ID,
                    "next_reset": runtime.get("next_reset"), "reset_timezone": runtime.get("reset_timezone"),
                    "last_reset": last_reset, "reset_running": runtime.get("reset_running", False),
                    "audit_error": runtime.get("audit_error"), "disk_free_gb": round(disk.free / 1024**3, 2)}
        if section == "reviews":
            try:
                offset = int(query.get("offset", "0"))
                limit = int(query.get("limit", "20"))
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid page.")
            if offset < 0 or not 1 <= limit <= 50:
                raise web.HTTPBadRequest(text="Invalid page.")
            async with module._review_lock(self.context):
                pending = module.lib.pending.list_pending()
                items = sorted(pending.items(), key=lambda pair: pair[1].get("created_at", 0))
                reviews = []
                for sid, entry in items[offset:offset + limit]:
                    link = None
                    if module.REVIEW_CHAT_ID.startswith("-100") and entry.get("message_id"):
                        link = f"https://t.me/c/{module.REVIEW_CHAT_ID[4:]}/{int(entry['message_id'])}"
                    reviews.append({"id": sid, "text": entry.get("text", ""),
                                    "reason": entry.get("reason", ""), "moderation": entry.get("moderation", "legacy"),
                                    "created_at": entry.get("created_at"), "telegram_url": link,
                                    "files": [{"name": f.get("name", "Attachment"),
                                               "mime_type": f.get("mime_type", "application/octet-stream")}
                                              for f in entry.get("files", [])]})
            return {"items": reviews, "total": len(items), "offset": offset, "limit": limit}
        if section == "activity":
            return {"items": store.recent(), "retention_days": 30}
        if section == "providers":
            results = {row["name"]: row["result"] for row in runtime.get("provider_results", [])}
            return {"running": self.app.bot_data.get("provider_test_running", False),
                    "started": runtime.get("provider_test_started"), "finished": runtime.get("provider_test_finished"),
                    "items": [{"name": api["name"], "model": api["model"],
                               "configured": bool(os.getenv(api["key_env"], "").strip()),
                               "result": results.get(api["name"])} for api in module.lib.moderation.APIS]}
        if section == "admins":
            chat = await module._validate_review_destination(self.app.bot)
            if int(module.REVIEW_CHAT_ID) > 0:
                return {"chat_id": chat.id, "title": "Private admin chat", "items": [
                    {"id": chat.id, "name": chat.full_name or "Private admin", "username": chat.username,
                     "role": "Private admin"}]}
            members = await self.app.bot.get_chat_administrators(chat_id=chat.id)
            return {"chat_id": chat.id, "title": chat.title or "Private admin chat",
                    "items": [{"id": m.user.id, "name": m.user.full_name, "username": m.user.username,
                               "role": "Private admin" if int(module.REVIEW_CHAT_ID) > 0 else
                               ("Owner" if m.status == "creator" else "Administrator")}
                              for m in members if not m.user.is_bot
                              and (int(module.REVIEW_CHAT_ID) > 0 or m.status in ("creator", "administrator"))]}
        raise web.HTTPNotFound()

    async def action(self, action, body, user):
        module = self.module
        if action == "review":
            sid = body.get("id")
            requested = body.get("decision")
            decision = {"approve": "ok", "reject": "no"}.get(requested) if isinstance(requested, str) else None
            if not isinstance(sid, str) or not sid or len(sid) > 200 or decision is None:
                raise web.HTTPBadRequest(text="Invalid review action.")
            result = await module._decide_submission(self.context, sid, decision, user, source="dashboard")
            if result["status"] == "missing":
                raise web.HTTPConflict(text="This submission was already reviewed or cleared. Refresh the queue.")
            if result["status"] == "failed":
                raise web.HTTPBadGateway(text="Publishing failed. The submission remains pending; check the channel before retrying.")
            pending = result["pending"]
            if pending.get("message_id"):
                try:
                    await self.app.bot.edit_message_text(
                        chat_id=module.REVIEW_CHAT_ID, message_id=pending["message_id"],
                        text=module._decision_label(pending, decision, user), parse_mode="HTML", reply_markup=None)
                except TelegramError as error:
                    print(f"Dashboard decision saved; Telegram display edit failed: {type(error).__name__}", flush=True)
            return {"ok": True, "message": "Approved and published." if decision == "ok" else "Submission rejected."}
        if action == "test-providers":
            if self.app.bot_data.get("provider_test_running"):
                raise web.HTTPConflict(text="A provider test is already running.")
            now = time.time()
            previous = module._runtime(self.context).get("provider_test_started", 0)
            if now - previous < 60:
                raise web.HTTPTooManyRequests(text="Wait a minute between provider tests.")
            self.app.bot_data["provider_test_running"] = True
            self.app.create_task(self._test_providers(), name="dashboard-provider-test")
            return {"ok": True, "message": "Provider tests started. Results will update here."}
        if action == "reset":
            if body.get("confirmation") != "RESET":
                raise web.HTTPBadRequest(text="Type RESET to confirm deletion of Tally submissions and pending reviews.")
            result = await module._reset_submissions(self.context, actor=user, source="dashboard")
            if result["ok"]:
                message = f"Dashboard reset complete: {result['deleted']} Tally submissions deleted; tracker and pending reviews cleared."
            elif result["phase"] == "busy":
                raise web.HTTPConflict(text="A reset is already running.")
            elif result["phase"] == "tally":
                message = f"Dashboard Tally reset failed ({result['error']}). Local pending reviews were preserved."
            else:
                message = f"Dashboard reset failed while clearing local state ({result['error']}). Tally was cleared."
            await module._notify_daily_reset(self.context, message)
            if not result["ok"]:
                raise web.HTTPBadGateway(text=message)
            return {"ok": True, "message": message}
        raise web.HTTPNotFound()

    async def _test_providers(self):
        try:
            await self.module._probe_providers(self.context)
        finally:
            self.app.bot_data.pop("provider_test_running", None)
