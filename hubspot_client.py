"""
HubSpot data layer. Uses a Private App token (env: HUBSPOT_TOKEN).

Responsibilities:
  - fetch a ticket's properties
  - fetch associated engagements (emails, notes, calls) and conversation threads
  - assemble one chronological, cleaned transcript for the model
  - write the six QA properties back

Only the standard CRM v3 + Conversations APIs are used, so this works on any
portal with the scopes: tickets (r/w), crm.objects.contacts.read,
conversations.read.
"""

import os
import sys
import html
import re
import requests
from datetime import datetime, timezone

BASE = "https://api.hubapi.com"
TOKEN = os.environ.get("HUBSPOT_TOKEN", "")

# Association type ids from Ticket -> engagement objects (HubSpot defined ids).
# We fetch associations generically then batch-read each engagement type.
ENGAGEMENT_TYPES = ["emails", "notes", "calls"]

TICKET_PROPS = [
    "subject", "content", "hs_pipeline", "hs_pipeline_stage",
    "hs_ticket_priority", "createdate", "hs_lastmodifieddate",
]


def _headers():
    if not TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN not set")
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _warn(msg):
    """Surface API problems (e.g. a missing scope returning 403) on stderr
    instead of silently returning empty data. Silent empties masquerade as a
    thin ticket and skew scores low, hiding the real cause."""
    print(f"[hubspot_client] WARNING: {msg}", file=sys.stderr)


def _clean(text):
    """Strip HTML, signatures, quoted-reply noise, and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)          # strip tags
    text = html.unescape(text)
    # cut common quoted-reply markers so we don't re-feed whole threads
    for marker in [r"\nOn .* wrote:", r"\n-----Original Message-----",
                   r"\nFrom: .*Sent:", r"\n_{5,}", r"\n-{5,}",
                   r"\nGet Outlook for", r"\nSent from my ",
                   r"\n[A-Z]{2,} Restricted"]:
        text = re.split(marker, text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_ticket(ticket_id):
    url = f"{BASE}/crm/v3/objects/tickets/{ticket_id}"
    params = {"properties": ",".join(TICKET_PROPS)}
    r = requests.get(url, headers=_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("properties", {})


def _get_association_ids(ticket_id, to_type):
    url = f"{BASE}/crm/v4/objects/tickets/{ticket_id}/associations/{to_type}"
    r = requests.get(url, headers=_headers(), timeout=30)
    if r.status_code != 200:
        _warn(f"associations {to_type} for ticket {ticket_id} -> HTTP "
              f"{r.status_code}: {r.text[:200]}")
        return []
    return [row["toObjectId"] for row in r.json().get("results", [])]


def _batch_read(obj_type, ids, props):
    if not ids:
        return []
    url = f"{BASE}/crm/v3/objects/{obj_type}/batch/read"
    body = {"properties": props, "inputs": [{"id": str(i)} for i in ids]}
    r = requests.post(url, headers=_headers(), json=body, timeout=30)
    if r.status_code != 200:
        _warn(f"batch read {obj_type} ({len(ids)} ids) -> HTTP "
              f"{r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("results", [])


def _ts(props, *keys):
    for k in keys:
        v = props.get(k)
        if v:
            try:
                return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00"))
                except Exception:
                    pass
    return datetime.min.replace(tzinfo=timezone.utc)


def collect_engagements(ticket_id):
    """Return list of dicts: {when, kind, direction, text}."""
    items = []

    emails = _batch_read("emails", _get_association_ids(ticket_id, "emails"), [
        "hs_email_text", "hs_email_html", "hs_email_subject",
        "hs_email_direction", "hs_timestamp", "hs_createdate"])
    for e in emails:
        p = e.get("properties", {})
        body = _clean(p.get("hs_email_text") or p.get("hs_email_html"))
        if not body:
            continue
        direction = p.get("hs_email_direction", "")
        who = "CUSTOMER" if "INCOMING" in (direction or "").upper() else "AGENT"
        subj = p.get("hs_email_subject", "")
        items.append({"when": _ts(p, "hs_timestamp", "hs_createdate"),
                      "kind": "EMAIL", "who": who,
                      "text": (f"[Subject: {subj}] " if subj else "") + body})

    notes = _batch_read("notes", _get_association_ids(ticket_id, "notes"), [
        "hs_note_body", "hs_timestamp", "hs_createdate"])
    for n in notes:
        p = n.get("properties", {})
        body = _clean(p.get("hs_note_body"))
        if body:
            items.append({"when": _ts(p, "hs_timestamp", "hs_createdate"),
                          "kind": "INTERNAL_NOTE", "who": "AGENT", "text": body})

    calls = _batch_read("calls", _get_association_ids(ticket_id, "calls"), [
        "hs_call_body", "hs_call_title", "hs_timestamp", "hs_createdate"])
    for c in calls:
        p = c.get("properties", {})
        body = _clean(p.get("hs_call_body"))
        title = p.get("hs_call_title", "")
        if body or title:
            items.append({"when": _ts(p, "hs_timestamp", "hs_createdate"),
                          "kind": "CALL", "who": "AGENT",
                          "text": (f"[{title}] " if title else "") + body})

    items += _collect_conversations(ticket_id)
    items.sort(key=lambda x: x["when"])
    return items


def _collect_conversations(ticket_id):
    """Inbox threads (connected email / live chat) associated to the ticket.
    In this portal the customer-facing dialogue lives here, and each message
    carries actorType AGENT / VISITOR, which we trust directly."""
    out = []
    url = f"{BASE}/conversations/v3/conversations/threads"
    thread_ids = _get_association_ids(ticket_id, "conversations")
    for tid in thread_ids:
        m = requests.get(f"{url}/{tid}/messages", headers=_headers(), timeout=30)
        if m.status_code != 200:
            _warn(f"conversation thread {tid} messages -> HTTP "
                  f"{m.status_code}: {m.text[:200]}")
            continue
        for msg in m.json().get("results", []):
            # Real messages are messageType "CommonMessage"; skip system/bot noise.
            if not msg.get("isCommonMessageType", False):
                continue
            body = _clean(msg.get("text") or "")
            if not body:
                continue
            actor = (msg.get("actorType") or "").upper()
            if actor == "VISITOR":
                who = "CUSTOMER"
            elif actor == "AGENT":
                who = "AGENT"
            else:
                # fall back to sender-id prefix if actorType missing
                sender = (msg.get("senders") or [{}])[0].get("actorId", "")
                who = "CUSTOMER" if sender.startswith("V-") else "AGENT"
            out.append({"when": _parse_when(msg.get("createdAt")),
                        "kind": "CHAT", "who": who, "text": body})
    return out


def _parse_when(raw):
    """Conversation timestamps come back as epoch-ms integers here, but tolerate
    ISO strings too."""
    if raw is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


def build_transcript(ticket_props, engagements, max_chars=24000):
    """Assemble the model input. Truncates oldest content if huge, keeping the
    ticket summary and the most recent communications (which matter most)."""
    header = (
        f"TICKET SUBJECT: {ticket_props.get('subject','(none)')}\n"
        f"PRIORITY: {ticket_props.get('hs_ticket_priority','(none)')}\n"
        f"STAGE: {ticket_props.get('hs_pipeline_stage','(none)')}\n"
        f"DESCRIPTION: {_clean(ticket_props.get('content',''))[:2000]}\n"
        f"{'='*40}\nCOMMUNICATIONS (chronological):\n"
    )
    lines = []
    for e in engagements:
        when = e["when"].strftime("%Y-%m-%d %H:%M") if e["when"].year > 1 else "?"
        lines.append(f"[{when}] {e['who']} via {e['kind']}:\n{e['text']}\n")

    body = "\n".join(lines)
    budget = max_chars - len(header)
    if len(body) > budget:
        # keep the tail (most recent), which is weighted most heavily
        body = "...[earlier communications truncated]...\n" + body[-budget:]
    if not lines:
        body = "(No associated communications or notes were found on this ticket. Evidence is limited.)"
    return header + body


def write_scores(ticket_id, scores):
    """PATCH the six QA properties. `scores` is the parsed model JSON."""
    props = {
        "ask_before_close_score": int(scores["ask_before_close_score"]),
        "ask_before_close_reason": str(scores["ask_before_close_reason"])[:65000],
        "customer_sentiment_score": int(scores["customer_sentiment_score"]),
        "customer_sentiment_reason": str(scores["customer_sentiment_reason"])[:65000],
        "agent_heart_score": int(scores["agent_heart_score"]),
        "agent_heart_reason": str(scores["agent_heart_reason"])[:65000],
    }
    url = f"{BASE}/crm/v3/objects/tickets/{ticket_id}"
    r = requests.patch(url, headers=_headers(), json={"properties": props}, timeout=30)
    r.raise_for_status()
    return props
