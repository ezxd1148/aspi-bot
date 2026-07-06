"""AI moderation — classifies confessions as CLEAN or FLAGGED.

Rotates through available APIs automatically on failure.
"""

import os

import requests

# API configurations in priority order (free APIs first, paid last)
APIS = [
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "poolside/laguna-xs-2.1:free",  # free tier
        "timeout": 15,
        "extra_body": {"reasoning_effort": "none"},
        "extra_headers": {
            "HTTP-Referer": "https://github.com/aspi-bot",
            "X-Title": "aspi-bot",
        },
    },
    {
        "name": "nvidia",
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_env": "NVIDIA_API_KEY",
        "model": "mistralai/mistral-medium-3.5-128b",
        "timeout": 60,  # cold starts on large models can take 20-30s
        "extra_body": {"reasoning_effort": "none"},
    },
    {
        "name": "deepseek",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-pro",
        "timeout": 30,
        "extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "low"},
    },
]

SYSTEM_PROMPT = (
    "You are inside a Malaysian Broadcast Group for ASASIpintar Confession Broadcast Channel."
    "You are a content moderation filter. Analyze this anonymous confession "
    "for harmful or inappropriate content: violence, hate speech, self-harm, "
    "explicit sexual content, harassment, threats, or illegal activity.\n\n"
    "If the text is short, vague, or ambiguous, default to CLEAN. "
    "If you are not sure about the text respond with FLAGGED"
    "Only flag content that clearly contains or strongly suggests harm.\n\n"
    "If text contains curse words of any kind respond FLAGGED"
    "Respond with ONLY the word CLEAN or the word FLAGGED. "
    "Do NOT include any other text, explanation, punctuation, or whitespace."
)


def moderate_text(text: str) -> str:
    """Check text with AI moderation.

    Returns:
        'clean'   — safe to auto-broadcast
        'flagged' — needs manual review
        'error'   — all APIs failed (conservative: treat as flagged)
    """
    for api in APIS:
        key = os.getenv(api["key_env"])
        if not key:
            continue

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        headers.update(api.get("extra_headers", {}))

        payload = {
            "model": api["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Classify as CLEAN or FLAGGED. Reply with only that word:\n\n{text}",
                },
            ],
            "max_tokens": 500,
            "temperature": 0.1,
            **api.get("extra_body", {}),
        }

        try:
            resp = requests.post(
                api["url"],
                headers=headers,
                json=payload,
                timeout=api.get("timeout", 30),
            )

            if resp.status_code == 200:
                data = resp.json()
                reply = (
                    (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    )
                    .strip()
                    .upper()
                )

                # Fallback: DeepSeek reasoning models put answer in reasoning_content
                if not reply:
                    reasoning = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("reasoning_content", "")
                        or ""
                    )
                    if reasoning:
                        # Take the last line of reasoning as the likely answer
                        reply = reasoning.strip().upper().split("\n")[-1].strip()

                # Debug: log full response on empty/unexpected
                if not reply or ("CLEAN" not in reply and "FLAGGED" not in reply):
                    import json

                    print(
                        f"  AI ({api['name']}) raw response: {json.dumps(data, indent=2)[:500]}"
                    )

                if "CLEAN" in reply:
                    print(f"  AI ({api['name']}): CLEAN")
                    return "clean"
                elif "FLAGGED" in reply:
                    print(f"  AI ({api['name']}): FLAGGED → manual review")
                    return "flagged"
                else:
                    print(f"  AI ({api['name']}) unexpected reply: {reply!r}")
                    return "flagged"

            elif resp.status_code in (429, 402):
                print(f"  AI ({api['name']}): rate-limited / no credits, rotating...")
                continue
            else:
                print(f"  AI ({api['name']}) error {resp.status_code}, rotating...")
                continue

        except requests.RequestException as e:
            print(f"  AI ({api['name']}) connection error: {e}, rotating...")
            continue

    print("  All AI APIs exhausted — flagging for manual review")
    return "error"
