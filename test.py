import base64
import os

import requests
from dotenv import load_dotenv

load_dotenv()

invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
stream = False

API_KEY = os.getenv("NVIDIA_API_KEY")

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "text/event-stream" if stream else "application/json",
}

payload = {
    "model": "mistralai/mistral-medium-3.5-128b",
    "reasoning_effort": "none",
    "messages": [{"role": "user", "content": ""}],
    "max_tokens": 16384,
    "temperature": 0.70,
    "top_p": 1.00,
    "stream": stream,
}


response = requests.post(invoke_url, headers=headers, json=payload)

if stream:
    for line in response.iter_lines():
        if line:
            print(line.decode("utf-8"))
else:
    print(response.json())
