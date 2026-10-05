import json
import os
import tempfile

DATA_DIR = os.getenv("DATA_DIR") or os.path.join(
    os.path.dirname(__file__), "..", "..", "data"
)
PENDING_FILE = os.path.join(DATA_DIR, "pending.json")


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
        except json.JSONDecodeError, TypeError:
            return {}


def _save(data: dict) -> None:
    path = _ensure_data_dir()
    # A reader must see the old or new complete document, never a truncated write.
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as f:
        temporary = f.name
        try:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_pending(submission_id: str, text: str, files: list[dict]) -> None:
    """Store submission text and file attachments for callback lookup."""
    data = _load()
    data[submission_id] = {"text": text, "files": files}
    _save(data)


def get_pending(submission_id: str) -> dict | None:
    """Retrieve stored submission data. Returns {text, files} or None.

    Handles legacy string-only entries by wrapping them in the new format.
    """
    entry = _load().get(submission_id)
    if entry is None:
        return None
    if isinstance(entry, str):
        return {"text": entry, "files": []}
    return entry


def clear_all() -> None:
    """Remove all pending entries."""
    _save({})


def list_pending() -> dict:
    """Include legacy entries without rewriting or losing their data."""
    return {sid: ({"text": entry, "files": []} if isinstance(entry, str) else entry)
            for sid, entry in _load().items() if isinstance(entry, (str, dict))}


def update_pending(submission_id: str, **metadata) -> None:
    data = list_pending()
    if submission_id in data:
        data[submission_id].update(metadata)
        _save(data)


def remove_pending(submission_id: str) -> None:
    """Delete a submission from pending storage."""
    data = _load()
    data.pop(submission_id, None)
    _save(data)
