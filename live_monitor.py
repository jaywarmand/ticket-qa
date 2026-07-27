"""
Live-ticket monitoring: sideways detector + premature-closure gate.

Reuses the fetch/transcript plumbing from hubspot_client.py and the swappable
model layer approach from llm.py, but with the live prompts.

New Ticket properties written (create these in HubSpot first):
  Sideways detector:
    ticket_risk_score       number 0-5
    ticket_risk_flag        bool (checkbox)
    ticket_risk_reason      multi-line text
    sentiment_history       multi-line text  (JSON list of {"t":date,"s":score})
  Closure gate:
    premature_closure_flag     bool (checkbox)
    premature_closure_warning  multi-line text

CLI:
    python live_monitor.py risk <ticket_id> [--dry-run]
    python live_monitor.py closure <ticket_id> [--dry-run]
"""

import os
import sys
import json
import re
import requests
from datetime import date

import hubspot_client as hs
from live_prompts import RISK_PROMPT, CLOSURE_GATE_PROMPT

PROVIDER = os.environ.get("PROVIDER", "anthropic").lower()
HISTORY_MAX = 6  # how many prior sentiment points to keep

# In warn-only mode the gate never blocks; flip to True once you trust it and
# want the close actually halted (requires the workflow/webhook to honor it).
CLOSURE_BLOCKING = os.environ.get("CLOSURE_BLOCKING", "false").lower() == "true"


# ---------- model call (mirrors llm.py, parameterized by system prompt) ----------
def _call_model(system_prompt, transcript):
    if PROVIDER == "openai":
        key = os.environ["OPENAI_API_KEY"]
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model, "temperature": 0,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system_prompt},
                               {"role": "user", "content": transcript}]},
            timeout=90)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    else:
        key = os.environ["ANTHROPIC_API_KEY"]
        model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 1024, "temperature": 0,
                  "system": system_prompt,
                  "messages": [{"role": "user", "content": transcript}]},
            timeout=90)
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _extract_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.I | re.M).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e + 1]
    return json.loads(raw)


def _read_history(ticket_id):
    props = hs._batch_read  # noqa (kept explicit below)
    url = f"{hs.BASE}/crm/v3/objects/tickets/{ticket_id}"
    r = requests.get(url, headers=hs._headers(),
                     params={"properties": "sentiment_history"}, timeout=30)
    if r.status_code != 200:
        return []
    raw = r.json().get("properties", {}).get("sentiment_history")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


# ---------- RISK / SIDEWAYS ----------
def run_risk(ticket_id, dry_run=False):
    props = hs.get_ticket(ticket_id)
    engagements = hs.collect_engagements(ticket_id)
    history = _read_history(ticket_id)

    hist_str = (", ".join(f"{h['t']}:{h['s']}" for h in history)
                if history else "(none)")
    transcript = hs.build_transcript(props, engagements)
    transcript += f"\n\nPRIOR SENTIMENT HISTORY (oldest->newest): {hist_str}\n"

    data = _extract_json(_call_model(RISK_PROMPT, transcript))
    data["risk_score"] = max(0, min(5, int(data["risk_score"])))
    data["current_sentiment"] = max(1, min(5, int(data["current_sentiment"])))
    data["risk_flag"] = bool(data.get("risk_flag"))

    # append today's sentiment point, keep last N
    history.append({"t": date.today().isoformat(), "s": data["current_sentiment"]})
    history = history[-HISTORY_MAX:]

    if not dry_run:
        write_props = {
            "ticket_risk_score": data["risk_score"],
            "ticket_risk_flag": data["risk_flag"],
            "ticket_risk_reason": str(data["risk_reason"])[:65000],
            "sentiment_history": json.dumps(history),
        }
        _patch(ticket_id, write_props)
    data["_history"] = history
    return data


# ---------- PREMATURE CLOSURE GATE ----------
def run_closure_gate(ticket_id, dry_run=False):
    props = hs.get_ticket(ticket_id)
    engagements = hs.collect_engagements(ticket_id)
    transcript = hs.build_transcript(props, engagements)

    data = _extract_json(_call_model(CLOSURE_GATE_PROMPT, transcript))
    data["safe_to_close"] = bool(data.get("safe_to_close"))
    data["open_items"] = data.get("open_items") or []
    warning = data.get("warning", "") if not data["safe_to_close"] else ""

    if not dry_run:
        _patch(ticket_id, {
            "premature_closure_flag": (not data["safe_to_close"]),
            "premature_closure_warning": str(warning)[:65000],
        })

    # In blocking mode the caller (webhook) should read safe_to_close and, if
    # False, prevent/revert the stage change. Warn-only mode just records it.
    data["_blocking"] = CLOSURE_BLOCKING
    return data


def _patch(ticket_id, props):
    url = f"{hs.BASE}/crm/v3/objects/tickets/{ticket_id}"
    r = requests.patch(url, headers=hs._headers(),
                       json={"properties": props}, timeout=30)
    r.raise_for_status()


def main():
    if len(sys.argv) < 3:
        print("usage: python live_monitor.py risk|closure <ticket_id> [--dry-run]")
        sys.exit(1)
    mode, tid = sys.argv[1], sys.argv[2]
    dry = "--dry-run" in sys.argv
    if mode == "risk":
        out = run_risk(tid, dry_run=dry)
    elif mode == "closure":
        out = run_closure_gate(tid, dry_run=dry)
    else:
        print("mode must be 'risk' or 'closure'")
        sys.exit(1)
    print(json.dumps(out, indent=2))
    if dry:
        print("(dry-run: nothing written)")


if __name__ == "__main__":
    main()
