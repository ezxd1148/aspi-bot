"""AI moderation — classifies confessions as CLEAN or FLAGGED.

Rotates through available APIs automatically on failure.
"""

import json
import os
from pathlib import Path
from time import monotonic

import requests
from dotenv import load_dotenv

load_dotenv()

# API configurations in priority order (free APIs first, paid last)
APIS = [
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "openrouter/free",  # free tier
        "timeout": 15,
        # The free router may choose a model that cannot disable reasoning.
        "extra_body": {"reasoning": {"exclude": True}},
        "extra_headers": {
            "HTTP-Referer": "https://github.com/aspi-bot",
            "X-Title": "aspi-bot",
        },
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "qwen/qwen3.8-27b",  # available on Groq's Free plan
        "timeout": 30,
        "extra_body": {"reasoning_effort": "low", "include_reasoning": False},
    },
    {
        "name": "nvidia",
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_env": "NVIDIA_API_KEY",
        "model": "moonshotai/kimi-k3",
        "timeout": 60,  # cold starts on large models can take 20-30s
        "extra_body": {"reasoning_effort": "none"},
    },
    {
        "name": "deepseek",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
        "timeout": 30,
        "extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "low"},
    },
]

SYSTEM_PROMPT = (
   Path(__file__).parent / "Prompt" / "SYSTEM_PROMPT.md"
).read_text()


def _request_moderation(api: dict, text: str, key: str):
    """Use the same prompt, model, and request settings for moderation and probes."""
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

    return requests.post(
        api["url"],
        headers=headers,
        json=payload,
        timeout=api.get("timeout", 30),
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

        try:
            resp = _request_moderation(api, text, key)

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


def _probe_answer(data) -> tuple[str, str | None]:
    """Describe probe failures without exposing raw API output or error messages."""
    if not isinstance(data, dict):
        return "", "response is not a JSON object"
    if data.get("error"):
        error = data["error"]
        code = error.get("code") if isinstance(error, dict) else None
        detail = f" (code {code})" if type(code) is int else ""
        return "", f"API error in response body{detail}"
    choices = data.get("choices")
    if not isinstance(choices, list):
        return "", "missing or invalid choices array"
    if not choices:
        return "", "empty choices array"
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", "invalid choice object"
    finish = choice.get("finish_reason")
    if finish == "length":
        return "", "token limit reached (finish_reason=length)"
    if finish == "content_filter":
        return "", "provider content filter blocked the answer"
    if finish == "error":
        return "", "provider generation error"
    message = choice.get("message")
    if not isinstance(message, dict):
        return "", "missing or invalid message object"
    if message.get("refusal"):
        return "", "provider refused the classification"
    reply = message.get("content")
    if reply is None or reply == "":
        return "", "empty final answer"
    if not isinstance(reply, str):
        return "", "content is not a text string"
    return reply.strip(), None


def test_provider(api: dict) -> str:
    """Probe one provider without fallback or publishing any submissions."""
    key = os.getenv(api["key_env"])
    if not key:
        return f"SKIPPED - missing {api['key_env']}"

    started = monotonic()
    checks = []
    for text, expected in [
        ("Stress wei chem ni", "CLEAN"),
        ("Dia g#y, gurau je", "FLAGGED"),
    ]:
        try:
            resp = _request_moderation(api, text, key)
            if resp.status_code != 200:
                reasons = {
                    400: "invalid request or unsupported parameters",
                    401: "authentication failed",
                    403: "access denied",
                    402: "no credits",
                    429: "rate limited",
                }
                reason = reasons.get(resp.status_code, "API error")
                checks.append(f"{expected}: FAIL - HTTP {resp.status_code} ({reason})")
                break
            try:
                data = resp.json()
            except ValueError:
                checks.append(f"{expected}: FAIL - response is not valid JSON")
                continue
            answer, error = _probe_answer(data)
            if error:
                checks.append(f"{expected}: FAIL - {error}")
                continue
            # Require the actual final answer, not a classification guessed from reasoning.
            if answer == expected:
                checks.append(f"{expected}: PASS")
            elif answer in ("CLEAN", "FLAGGED"):
                checks.append(f"{expected}: FAIL - returned {answer}")
            else:
                checks.append(f"{expected}: FAIL - invalid or empty reply")
        except requests.Timeout:
            checks.append(f"{expected}: FAIL - timed out")
            break
        except requests.RequestException:
            checks.append(f"{expected}: FAIL - connection/request error")
            break
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            checks.append(f"{expected}: FAIL - malformed response")

    return "; ".join(checks) + f" ({monotonic() - started:.1f}s)"
