from pathlib import Path


SYSTEM_PROMPT = Path(__file__).parent / "Prompt" / "SYSTEM_PROMPT.md"

print(SYSTEM_PROMPT.read_text())
