import requests

api = "tly-obY3CPK7ZY030kyKMoUKDqKc5tR74k8R"
form = "7R52G0"
submissionId = "1WJEAGO"

url = f"https://api.tally.so/forms/{form}/submissions/{submissionId}"

headers = {"Authorization": f"Bearer {api}"}

response = requests.get(url, headers=headers)

print(response.text)
