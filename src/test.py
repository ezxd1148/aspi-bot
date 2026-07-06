import json
import os

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

API_KEY = os.getenv("OPENROUTER_API_KEY")

from openai import OpenAI

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=f"{API_KEY}",
)

# First API call with reasoning
response = client.chat.completions.create(
    model="poolside/laguna-xs-2.1:free",
    messages=[
        {"role": "user", "content": "How many r's are in the word 'strawberry'?"}
    ],
    extra_body={"reasoning": {"enabled": True}},
)

# Extract the assistant message with reasoning_details
response = response.choices[0].message

# Preserve the assistant message with reasoning_details
messages = [
    {"role": "user", "content": "How many r's are in the word 'strawberry'?"},
    {
        "role": "assistant",
        "content": response.content,
        "reasoning_details": response.reasoning_details,  # Pass back unmodified
    },
    {"role": "user", "content": "Are you sure? Think carefully."},
]

# Second API call - model continues reasoning from where it left off
response2 = client.chat.completions.create(
    model="poolside/laguna-xs-2.1:free",
    messages=messages,
    extra_body={"reasoning": {"enabled": True}},
)
