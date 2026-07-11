import json
import os

DATA_DIR = os.getenv("DATA_DIR") or os.path.join(
    os.path.dirname(__file__), "..", "..", "data"
)
TRACKER_FILE = os.path.join(DATA_DIR, "processed_ids.json")


def _ensure_data_dir() -> str:
    """Create the data directory if it doesn't exist, return the file path."""
    os.makedirs(os.path.dirname(TRACKER_FILE), exist_ok=True)
    return TRACKER_FILE


def load_processed() -> set[str]:
    """Load the set of already-processed submission IDs."""
    path = _ensure_data_dir()
    if not os.path.exists(path):
        return set()
    with open(path, "r") as f:
        try:
            return set(json.load(f))
        except json.JSONDecodeError, TypeError:
            return set()


def save_processed(ids: set[str]) -> None:
    """Persist the set of processed submission IDs."""
    path = _ensure_data_dir()
    with open(path, "w") as f:
        json.dump(sorted(ids), f, indent=2)


def mark_processed(submission_id: str) -> None:
    """Add a submission ID to the processed set."""
    ids = load_processed()
    ids.add(submission_id)
    save_processed(ids)


def reset() -> None:
    """Clear all processed IDs."""
    save_processed(set())


def is_processed(submission_id: str) -> bool:
    """Check whether a submission ID has already been processed."""
    return submission_id in load_processed()
