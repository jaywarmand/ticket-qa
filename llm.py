"""
Swappable model layer. Set PROVIDER=openai or PROVIDER=anthropic.

Env:
  PROVIDER          openai | anthropic   (default: anthropic)
  OPENAI_API_KEY    for OpenAI
  OPENAI_MODEL      default gpt-4o-mini
  ANTHROPIC_API_KEY for Claude
  ANTHROPIC_MODEL   default claude-haiku-4-5-20251001

Both providers are asked for JSON only. We parse defensively and validate the
score ranges so a bad response can't write garbage to the CRM.
"""

import os
import json
import re
import requests

from prompt import SYSTEM_PROMPT

PROVIDER = os.environ.get("PROVIDER", "anthropic").lower()

_RANGES = {
    "ask_before_close_score": (0, 5),
    "customer_sentiment_score": (1, 5),
    "agent_heart_score": (1, 5),
}
_REASON_KEYS = [
    "ask_before_close_reason",
    "customer_sentiment_reason",
    "agent_heart_reason",
]


def _extract_json(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    # grab the outermost object if the model added stray text
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return json.loads(raw)


def _validate(data):
    for k, (lo, hi) in _RANGES.items():
        if k not in data:
            raise ValueError(f"missing key {k}")
        v = int(data[k])
        if not (lo <= v <= hi):
            raise ValueError(f"{k}={v} out of range {lo}-{hi}")
        data[k] = v
    for k in _REASON_KEYS:
        if not data.get(k):
            raise ValueError(f"missing/empty {k}")
        data[k] = str(data[k]).strip()
    return data


def _call_openai(transcript):
    key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
        }, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_anthropic(transcript):
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key,
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": 1024,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": transcript}],
        }, timeout=90)
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def score_ticket(transcript):
    raw = _call_openai(transcript) if PROVIDER == "openai" else _call_anthropic(transcript)
    return _validate(_extract_json(raw))
