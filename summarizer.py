"""Claude API integration — summarizes filing text into summary + key bullets."""

import os
import re
import time

import anthropic

_MAX_RETRIES = 3
_RETRY_WAIT  = 60  # seconds

_SYSTEM_PROMPT = (
    "You are a financial analyst assistant. You will be given the text of an SEC filing. "
    "Respond with exactly two sections:\n\n"
    "SUMMARY:\n"
    "A 2-3 sentence summary of the filing's key content and significance.\n\n"
    "KEY BULLETS:\n"
    "3-5 bullet points (each starting with '- ') highlighting the most important facts, "
    "figures, or developments in the filing."
)


def summarize_filing(text: str, model: str = "claude-sonnet-4-6") -> dict:
    """Send filing text to Claude and return a structured summary.

    Retries up to _MAX_RETRIES times on RateLimitError, waiting _RETRY_WAIT
    seconds between attempts.

    Returns:
        {"summary": str, "key_bullets": list[str]}
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with client.messages.stream(
                model=model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Please summarize this SEC filing:\n\n{text}"}],
            ) as stream:
                response_text = stream.get_final_message().content[0].text
            return _parse_response(response_text)
        except anthropic.RateLimitError:
            if attempt == _MAX_RETRIES:
                raise
            print(f"      Rate limit hit — waiting {_RETRY_WAIT}s before retry {attempt}/{_MAX_RETRIES}...")
            time.sleep(_RETRY_WAIT)


def _parse_response(text: str) -> dict:
    """Parse the SUMMARY / KEY BULLETS response into a structured dict."""
    summary = ""
    key_bullets = []

    summary_match = re.search(r"SUMMARY:\s*(.+?)(?=KEY BULLETS:|$)", text, re.DOTALL | re.IGNORECASE)
    if summary_match:
        summary = summary_match.group(1).strip()

    bullets_match = re.search(r"KEY BULLETS:\s*(.+)", text, re.DOTALL | re.IGNORECASE)
    if bullets_match:
        bullets_block = bullets_match.group(1).strip()
        key_bullets = [
            line.lstrip("-").strip()
            for line in bullets_block.splitlines()
            if line.strip().startswith("-")
        ]

    return {"summary": summary, "key_bullets": key_bullets}
