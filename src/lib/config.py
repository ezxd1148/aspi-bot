"""Load configuration from this checkout, with one explicit review destination."""

import os
from pathlib import Path
import re

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_environment() -> None:
    """The deployed checkout's .env takes precedence over inherited values."""
    if ENV_FILE.is_file():
        seen = set()
        for line in ENV_FILE.read_text().splitlines():
            match = re.match(r"\s*(?:export\s+)?(ADMIN_GROUP_ID|ADMIN_CHAT_ID)\s*=", line)
            if match:
                name = match[1]
                if name in seen:
                    raise ValueError(f"Duplicate {name} in {ENV_FILE}; keep one entry.")
                seen.add(name)
        load_dotenv(ENV_FILE, override=True)


def review_destination() -> tuple[str, str]:
    """Prefer explicit group configuration; never fall back from an invalid group."""
    name = "ADMIN_GROUP_ID" if "ADMIN_GROUP_ID" in os.environ else "ADMIN_CHAT_ID"
    value = os.getenv(name, "").strip()
    if not re.fullmatch(r"-?[0-9]+", value) or int(value) == 0:
        raise ValueError(f"{name} must be a numeric Telegram chat ID. Run /start in the review group.")
    if name == "ADMIN_GROUP_ID" and int(value) >= 0:
        raise ValueError("ADMIN_GROUP_ID must be a negative group ID, not a user's ID.")
    return str(int(value)), name
