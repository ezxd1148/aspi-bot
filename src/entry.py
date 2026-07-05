import json

from js import Headers, Request, Response, fetch


async def on_fetch(request, env):
    # 1. Restrict to POST
    if request.method != "POST":
        return Response.new("Method Not Allowed", status=405)

    try:
        body = await request.text()
        payload = json.loads(body)

        # 2. Extract webhook payload
        tally_data = payload.get("data", {})
        fields = tally_data.get("fields", [])

        confession_text = ""
        for field in fields:
            if field.get("type") in ["TEXTAREA", "INPUT_TEXT"]:
                confession_text = field.get("value", "")
                if confession_text:
                    break

        if not confession_text:
            return Response.new(
                json.dumps({"error": "No confession text found"}), status=400
            )

        # 3. Get Environment Variables
        bot_token = getattr(env, "TELEGRAM_BOT_TOKEN", None)
        channel_id = getattr(env, "TELEGRAM_CHANNEL_ID", None)

        if not bot_token or not channel_id:
            return Response.new(
                json.dumps({"error": "Missing Environment Variables"}), status=500
            )

        # 4. Dispatch to Telegram
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

        # 5. Handle Telegram's response
        if tg_response.status != 200:
            tg_text = await tg_response.text()
            return Response.new(
                json.dumps(
                    {"error": "Telegram API Rejected Message", "details": tg_text}
                ),
                status=500,
            )

        return Response.new(json.dumps({"status": "success"}), status=200)

    except Exception as e:
        return Response.new(
            json.dumps({"error": "Internal crash", "details": str(e)}), status=500
        )
