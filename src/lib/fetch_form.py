import requests


def fetch_data(api, form_id):
    URL = f"https://api.tally.so/forms/{form_id}/submissions"

    headers = {"Authorization": f"Bearer {api}", "Content-Type": "application/json"}

    response = requests.get(URL, headers=headers)

    if response.status_code == 200:
        data = response.json()
        total = data.get("totalNumberOfSubmissionsPerFilter", {}).get("all", 0)
        print(
            f"Fetched {len(data.get('submissions', []))} submissions (total: {total})."
        )
        return data
    else:
        print(f"Error: {response.status_code}", response.text)
        return None


def extract_submission(data: dict) -> tuple[str, list[dict]]:
    """Extract text and file attachments from a submission wrapper.

    Handles both flat list-endpoint format and nested single-endpoint format.

    Returns (text, files) where each file dict has: name, url, mime_type.
    """
    # Try to find the responses array across possible nesting levels
    responses = data.get("responses") or data.get("submission", {}).get("responses", [])

    text = ""
    files: list[dict] = []

    for res in responses:
        answer = res.get("answer")
        if not answer:
            continue

        if isinstance(answer, list):
            # FILE_UPLOAD: answer is a list of file objects
            for f in answer:
                files.append(
                    {
                        "name": f.get("name", "unnamed"),
                        "url": f.get("url", ""),
                        "mime_type": f.get("mimeType", "application/octet-stream"),
                    }
                )
        elif isinstance(answer, str) and answer.strip():
            text = answer

    # Fallback: list endpoint flat format (answer directly on the item)
    if not text and not files:
        direct = data.get("answer")
        if isinstance(direct, str):
            text = direct
        elif isinstance(direct, list):
            for f in direct:
                files.append(
                    {
                        "name": f.get("name", "unnamed"),
                        "url": f.get("url", ""),
                        "mime_type": f.get("mimeType", "application/octet-stream"),
                    }
                )

    return text, files
