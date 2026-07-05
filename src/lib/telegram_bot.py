import requests


def send_message(bot_token: str, channel_id: str, text: str) -> bool:
    """Send a message to a Telegram channel via the Bot API.

    Returns True on success, False on failure.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    payload = {
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(url, json=payload, timeout=15)

    if response.status_code == 200:
        print(f"Sent to Telegram: {text[:80]}...")
        return True
    else:
        print(f"Telegram error {response.status_code}: {response.text}")
        return False
