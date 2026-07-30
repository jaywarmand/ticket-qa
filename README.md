# HubSpot Ticket QA — self-hosted (replaces Breeze)

Scores each ticket on three axes and writes six properties back to the same
ticket. One merged model call per ticket. Provider-swappable (OpenAI / Claude).

Writes: `ask_before_close_score`, `ask_before_close_reason`,
`customer_sentiment_score`, `customer_sentiment_reason`,
`agent_heart_score`, `agent_heart_reason`.
(All six confirmed to already exist in your portal, correct types.)

## Files
- `prompt.py` — merged scoring rubric, JSON-output contract (no CRM-write text)
- `hubspot_client.py` — fetch ticket + emails/notes/calls/chats, build transcript, write back
- `llm.py` — OpenAI + Anthropic callers, defensive JSON parse + range validation
- `score_ticket.py` — CLI + webhook server

## 1. HubSpot Private App
Settings → Integrations → Private Apps → Create. Scopes:
- `tickets` (read + write)
- `crm.objects.contacts.read`
- `conversations.read`
- `sales-email-read` (read logged email engagement bodies — REQUIRED, or
  emails 403 and the transcript silently loses all email content)

Copy the access token → env `HUBSPOT_TOKEN`.
Copy the app's Client Secret → env `HUBSPOT_CLIENT_SECRET` (for webhook signature check).

## 2. Environment
```
export HUBSPOT_TOKEN=pat-...
export HUBSPOT_CLIENT_SECRET=...

# pick one provider:
export PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-haiku-4-5-20251001   # cheapest good option

# or:
export PROVIDER=openai
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o-mini

# stage that means "ready to close" (from your ticket pipeline):
export TRIGGER_STAGE_ID=<stage_id>
```

## 3. Test on one real ticket (no write)
```
pip install -r requirements.txt
python score_ticket.py <ticket_id> --dry-run
```
Confirms fetch + transcript + model + parsing. Drop `--dry-run` to write.

## 4. Wire the webhook (stage-change trigger)
```
python score_ticket.py --serve      # listens on :8000/webhook
```
Then either:
- **Private App webhooks:** subscribe to `ticket.propertyChange` on
  `hs_pipeline_stage`, target URL `https://<host>/webhook`, or
- **Workflow webhook:** enroll tickets when stage = your pre-close stage,
  action = Send webhook (POST) to `https://<host>/webhook`.

`TRIGGER_STAGE_ID` filters events so you only spend model calls on the right
stage. Signature is verified against `HUBSPOT_CLIENT_SECRET`.

## Notes / tuning
- **Cost:** one call, temp 0, ~a few thousand input tokens. Haiku / gpt-4o-mini
  = fractions of a cent per ticket.
- **Transcript budget:** `build_transcript(max_chars=24000)` keeps the newest
  content (weighted most heavily) and truncates the oldest. Raise if you use a
  large-context model.
- **Cleaning:** signatures, HTML, and quoted-reply chains are stripped in
  `_clean()`. Adjust the markers there if your mail footers differ.
- **Conversations vs engagements:** inbox chat/email threads are pulled via the
  ticket's associated `conversations`; logged emails/calls/notes via engagement
  associations. If your team works only in the Conversations inbox and threads
  aren't associated to tickets, associate them (or the transcript will be thin
  and scores will be conservative by design).
- **Idempotency:** re-running overwrites the six props; safe to reprocess.

---

# Live monitoring (active tickets)

Two additional modes for OPEN tickets, separate from the retrospective QA above.
These use purpose-built prompts (`live_prompts.py`) — do NOT run the QA rubric on
active tickets (Ask-Before-Close is meaningless mid-conversation and will false-alarm).

Files: `live_prompts.py`, `live_monitor.py`.

## New Ticket properties to create
Sideways detector:
- `ticket_risk_score` — Number (0–5)
- `ticket_risk_flag` — Single checkbox (boolean)
- `ticket_risk_reason` — Multi-line text
- `sentiment_history` — Multi-line text (JSON, trajectory store — no external DB)

Premature-closure gate:
- `premature_closure_flag` — Single checkbox (boolean)
- `premature_closure_warning` — Multi-line text

(You already have `customer_confirmed_closing` and `askedtoclosed` — the gate
complements those; it detects open items the agent may have missed.)

## Sideways detector
```
python live_monitor.py risk <ticket_id> --dry-run
```
Trigger: on any new ticket activity (workflow: enroll on a customer email/chat
received AND on agent messages), so both risk and the live HEART score below
stay current. It writes a 0–5 risk score + flag, and appends today's sentiment
to `sentiment_history` (last 6 points kept). The model flags on *trajectory*
(e.g. 4→3→2) and escalation language, not just absolute low scores — so you
catch tickets sliding before they hit rock bottom.

Live Agent HEART: the same call also scores the agent's handling *so far* and
writes it to `agent_heart_score` / `agent_heart_reason` — the SAME properties the
retrospective QA uses. So HEART updates live while the ticket is open; the QA run
at close then overwrites it with the final authoritative value. The live prompt
does NOT penalize for the absence of closure/confirmation (the ticket is ongoing).
Ask-Before-Close is deliberately NOT scored live (meaningless mid-conversation).

## Premature-closure gate
```
python live_monitor.py closure <ticket_id> --dry-run
```
Trigger: on stage-change-to-close (same event as retrospective QA — run the gate
FIRST). It checks whether the customer's last message has unanswered questions or
unmet requests. Writes `premature_closure_flag` + `premature_closure_warning`.

Warn-now / block-later:
- Default `CLOSURE_BLOCKING=false` → warn only: records the flag, lets close proceed.
- Set `CLOSURE_BLOCKING=true` once trusted → the webhook handler reads
  `safe_to_close`; if false it should revert the stage / block the close. (Wire
  this into your webhook once you've watched the warnings for a while and trust
  the precision.)

## Suggested full pipeline
1. Customer inbound  → `live_monitor.py risk`   (sideways early warning)
2. Stage → pre-close → `live_monitor.py closure` (gate; warn, later block)
3. Stage → closed    → `score_ticket.py`         (retrospective QA scoring)
All three share the same fetch/transcript plumbing and provider settings.
