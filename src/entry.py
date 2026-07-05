import json

from js import Headers, Request, fetch
from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # 1. Restrict endpoint to POST requests
        if request.method != "POST":
            return Response.new("Method Not Allowed", status=405)

        try:
            # 2. Extract and parse the exact Tally webhook payload
            body = await request.text()
            payload = json.loads(body)

            # Dig into 'data' -> 'fields' based on the screenshot
            tally_data = payload.get("data", {})
            fields = tally_data.get("fields", [])

            confession_text = ""
            for field in fields:
                # Target the text field containing the submission
                if field.get("type") in ["TEXTAREA", "INPUT_TEXT"]:
                    confession_text = field.get("value", "")
                    if confession_text:
                        break

            # If we still can't find anything, return an informative error
            if not confession_text:
                return Response.json(
                    {"error": "No confession text found in fields"}, status=400
                )

            # 3. Retrieve secrets securely from Cloudflare
            bot_token = self.env.TELEGRAM_BOT_TOKEN
            channel_id = self.env.TELEGRAM_CHANNEL_ID

            # 4. Format and dispatch payload to Telegram API
            telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            formatted_message = f"📢 **New Batch Confession:**\n\n{confession_text}"

            telegram_payload = json.dumps(
                {
                    "chat_id": channel_id,
                    "text": formatted_message,
                    "parse_mode": "Markdown",
                }
            )

            headers = Headers.new({"Content-Type": "application/json"})
            tg_request = Request.new(
                telegram_url, method="POST", headers=headers, body=telegram_payload
            )

            # Dispatch to Telegram
            await fetch(tg_request)

            return Response.json(
                {"status": "success", "message": "Confession broadcasted!"}
            )

        except Exception as e:
            return Response.json({"error": str(e)}, status=500)
