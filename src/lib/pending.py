import json
import os

PENDING_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "pending.json"
)


def _ensure_data_dir() -> str:
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    return PENDING_FILE


def _load() -> dict:
    path = _ensure_data_dir()
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except (json.JSONDecodeError, TypeError):
            return {}


def _save(data: dict) -> None:
    path = _ensure_data_dir()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def save_pending(submission_id: str, text: str) -> None:
    """Store submission text for later callback lookup."""
    data = _load()
    data[submission_id] = text
    _save(data)


def get_pending(submission_id: str) -> str | None:
    """Retrieve stored submission text by ID."""
    return _load().get(submission_id)


def remove_pending(submission_id: str) -> None:
    """Delete a submission from pending storage."""
    data = _load()
    data.pop(submission_id, None)
    _save(data)
