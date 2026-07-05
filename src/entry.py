import json

from js import Headers, Request, fetch
from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # 1. Restrict endpoint to POST requests (sent by Tally)
        if request.method != "POST":
            return Response.new("Method Not Allowed", status=405)

        try:
            # 2. Extract and parse the Tally payload
            body = await request.text()
            data = json.loads(body)

            submission = data.get("submission", {})
            responses = submission.get("responses", [])

            confession_text = ""
            for resp in responses:
                confession_text = resp.get("answer", "")
                if confession_text:
                    break

            if not confession_text:
                return Response.json({"error": "No confession found"}, status=400)

            # 3. Retrieve secrets securely from Cloudflare Environment Bindings
            bot_token = self.env.TELEGRAM_BOT_TOKEN
            channel_id = self.env.TELEGRAM_CHANNEL_ID

            # 4. Format and dispatch payload to Telegram API
            telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = json.dumps(
                {
                    "chat_id": channel_id,
                    "text": f"📢 **New Batch Confession:**\n\n{confession_text}",
                    "parse_mode": "Markdown",
                }
            )

            headers = Headers.new({"Content-Type": "application/json"})
            tg_request = Request.new(
                telegram_url, method="POST", headers=headers, body=payload
            )

            # Fire the request out across the edge network
            await fetch(tg_request)

            return Response.json({"status": "success"})

        except Exception as e:
            return Response.json({"error": str(e)}, status=500)
