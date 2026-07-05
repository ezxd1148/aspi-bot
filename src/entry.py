import json

from js import Headers, Request, fetch
from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method != "POST":
            return Response.new("Method Not Allowed", status=405)

        try:
            body = await request.text()
            payload = json.loads(body)

            tally_data = payload.get("data", {})
            fields = tally_data.get("fields", [])

            confession_text = ""
            for field in fields:
                if field.get("type") in ["TEXTAREA", "INPUT_TEXT"]:
                    confession_text = field.get("value", "")
                    if confession_text:
                        break

            if not confession_text:
                return Response.json({"error": "No confession text found"}, status=400)

            # Safely extract environment variables
            bot_token = getattr(self.env, "TELEGRAM_BOT_TOKEN", None)
            channel_id = getattr(self.env, "TELEGRAM_CHANNEL_ID", None)

            if not bot_token or not channel_id:
                return Response.json(
                    {
                        "error": "Missing Cloudflare Environment Variables",
                        "details": f"Bot Token Exists: {bool(bot_token)}, Channel ID Exists: {bool(channel_id)}",
                    },
                    status=500,
                )

            # Formulate the outbound request manually
            telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            telegram_payload = json.dumps(
                {
                    "chat_id": channel_id,
                    "text": f"📢 New Batch Confession:\n\n{confession_text}",
                }
            )

            headers = Headers.new({"Content-Type": "application/json"})
            tg_request = Request.new(
                telegram_url, method="POST", headers=headers, body=telegram_payload
            )

            tg_response = await fetch(tg_request)
            tg_status = tg_response.status
            tg_text = await tg_response.text()

            if tg_status != 200:
                return Response.json(
                    {
                        "error": "Telegram API Rejected Message",
                        "status_code": tg_status,
                        "details": tg_text,
                    },
                    status=500,
                )

            return Response.json({"status": "success"})

        except Exception as e:
            return Response.json(
                {"error": "Internal crash", "details": str(e)}, status=500
            )
