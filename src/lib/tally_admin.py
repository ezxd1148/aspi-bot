"""Admin utilities for the Tally API."""

import requests


def delete_all_submissions(api_key: str, form_id: str) -> int:
    """Delete all submissions from a Tally form. Returns count deleted.

    Paginates through all submissions, deletes each by ID.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    base_url = f"https://api.tally.so/forms/{form_id}/submissions"

    # 1. Collect all submission IDs (paginated)
    all_ids: list[str] = []
    page = 1
    limit = 100

    while True:
        params = {"page": page, "limit": limit}
        resp = requests.get(base_url, headers=headers, params=params)
        if resp.status_code != 200:
            print(f"Tally fetch failed during reset: {resp.status_code}")
            break

        data = resp.json()
        submissions = data.get("submissions", [])
        if not submissions:
            break

        all_ids.extend(sub["id"] for sub in submissions if sub.get("id"))

        if not data.get("hasMore"):
            break
        page += 1

    if not all_ids:
        print("No submissions to delete.")
        return 0

    # 2. Delete each submission
    deleted = 0
    for sid in all_ids:
        del_url = f"{base_url}/{sid}"
        resp = requests.delete(del_url, headers=headers)
        if resp.status_code in (200, 204):
            deleted += 1
        else:
            print(f"  Failed to delete {sid}: {resp.status_code}")

    print(f"Deleted {deleted}/{len(all_ids)} submissions from Tally.")
    return deleted
