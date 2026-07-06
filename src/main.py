import os
import sys

from dotenv import load_dotenv

import lib.fetch_form
import lib.telegram_bot
import lib.tracker

# ── Environment ───────────────────────────────────────────────────────────────

load_dotenv()

TALLY_API_KEY = os.getenv("TALLY_API_KEY")
FORM_ID = os.getenv("FORM_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

REQUIRED_VARS = {
    "TALLY_API_KEY": TALLY_API_KEY,
    "FORM_ID": FORM_ID,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
}

missing = [name for name, val in REQUIRED_VARS.items() if val is None]
if missing:
    print(f"Missing environment variables: {', '.join(missing)}")
    sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    data = lib.fetch_form.fetch_data(TALLY_API_KEY, FORM_ID)
    if data is None:
        print("Failed to fetch data from Tally.")
        return

    submissions = data.get("submissions", [])
    if not submissions:
        print("No submissions found.")
        return

    new_count = 0
    for submission in submissions:
        submission_id = submission.get("id")
        if not submission_id:
            continue

        if lib.tracker.is_processed(submission_id):
            continue

        text, files = lib.fetch_form.extract_submission(submission)
        if not text and not files:
            continue

        print(f"Text: {text}")
        for f in files:
            print(f"  File: {f['name']} ({f['mime_type']})")

        success = lib.telegram_bot.send_message(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, text
        )
        if success:
            lib.tracker.mark_processed(submission_id)
            new_count += 1

    if new_count == 0:
        print("No new confessions to broadcast.")
    else:
        print(f"Broadcasted {new_count} new confession(s).")


if __name__ == "__main__":
    main()
