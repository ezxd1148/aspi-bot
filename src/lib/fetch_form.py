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


def get_answers(data):
    """Extract answers from a single submission item.

    `data` is one element from the API response's `submissions` array,
    with `responses` directly on it.
    """
    responses = data.get("responses", [])
    for res in responses:
        answer = res.get("answer", "")
        if answer:
            return answer

    return "No text found."
