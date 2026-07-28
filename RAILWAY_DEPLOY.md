# Deploying to Railway + HubSpot — step by step

Takes you from "code on GitHub" to "HubSpot calls your live service." No prior
Railway experience assumed. ~1 hour the first time.

This project authenticates to HubSpot with a **Service Key** (not a Private App),
so there is NO client secret / signature. The webhook is protected instead by a
shared `WEBHOOK_KEY` carried in the URL (`?key=...`).

---

## Before you start — gather these
- HubSpot **Service Key** token -> `HUBSPOT_TOKEN`
- Your model key — **Anthropic** (`ANTHROPIC_API_KEY`) or **OpenAI** (`OPENAI_API_KEY`)
- A long random **webhook key** — generate one with:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`

### Required Service Key scopes
In HubSpot -> Settings -> Integrations -> your Service Key -> Scopes, enable:
- `tickets` (read + write) — it writes scores back
- `crm.objects.contacts.read`
- `conversations.read`
- `sales-email-read` — REQUIRED. Without it, `emails/batch/read` returns 403 and
  the transcript silently loses ALL email content, skewing scores low.

---

## Phase 1 — Host the service on Railway (from GitHub)

The repo already lives on GitHub. Every push auto-redeploys.

### 1. Create the Railway project
- railway.app -> **New Project** -> **Deploy from GitHub repo**.
- Grant Railway access to the repo, then select it.
- Railway auto-detects `railway.json` + `Procfile` and builds the web service
  (`python score_ticket.py --serve`). No build config needed.

### 2. Add environment variables
Service -> **Variables** -> **Raw Editor**, paste and fill in real values:
```
HUBSPOT_TOKEN=pat-na1-...
PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
ANTHROPIC_MAX_TOKENS=1536
WEBHOOK_KEY=paste-your-generated-key-here
CLOSURE_BLOCKING=false
```
- **Do NOT** set `PORT` — Railway sets it and the code reads it.
- No `HUBSPOT_CLIENT_SECRET` needed (Service Key uses the `?key=` method).
- For OpenAI instead: set `PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL`,
  and optionally `OPENAI_MAX_TOKENS`.

### 3. Give it a public URL
Service -> **Settings** -> **Networking** -> **Generate Domain**. You get e.g.
`https://ticket-qa-production.up.railway.app`. Copy it.

### 4. Confirm it's alive (before touching HubSpot)
Visit `https://<domain>/health`. Expect:
`{"ok": true, "provider": "anthropic", "modes": ["qa", "closure", "risk"]}`

---

## Phase 2 — Connect HubSpot workflows

All three modes share ONE service; `?mode=` on the URL selects the logic. Build
three ticket-based workflows differing only in the enrollment stage and the URL.
Confirmed stage IDs for this portal's HD pipeline (144473189):

| Workflow           | Enroll when stage =        | Webhook URL (POST)                                        |
|--------------------|----------------------------|----------------------------------------------------------|
| Retrospective QA   | Closed (245698182)         | `https://<domain>/webhook?key=YOUR_KEY`                  |
| Closure gate       | Resolved (245844492)       | `https://<domain>/webhook?mode=closure&key=YOUR_KEY`    |
| Sideways detector  | Customer Responded (245705643) | `https://<domain>/webhook?mode=risk&key=YOUR_KEY`   |

Replace `<domain>` with your Railway domain and `YOUR_KEY` with the exact
`WEBHOOK_KEY` value from Railway. A mismatch returns HTTP 401.

For each: HubSpot -> Automation -> **Workflows** -> Create -> **Ticket-based** ->
enrollment trigger = *Ticket stage is [that stage]* -> action **Send a webhook**
-> Method **POST**, URL from the table -> webhook auth **None** (the `?key=` is
the auth) -> turn it **On**.

Leave `TRIGGER_STAGE_ID` unset — the workflow does the stage filtering.

> Webhook mode ALWAYS writes (there is no dry-run over the webhook). Validate
> with the local CLI first: `python score_ticket.py <ticket_id> --dry-run`.

---

## Recommended launch order
1. Deploy, confirm `/health`.
2. Dry-run 10-15 real tickets locally and eyeball the output.
3. Turn on ONE workflow — Retrospective QA (simplest). Watch a day.
4. Add the Closure gate (stays warn-only while `CLOSURE_BLOCKING=false`).
5. Add the Sideways detector.
6. Once you trust the closure gate, set `CLOSURE_BLOCKING=true` and wire the
   workflow to honor `safe_to_close` (revert the stage when false).

---

## Troubleshooting
- **`/health` won't load / app restarts:** check **Deploy Logs** (service ->
  Deployments -> View Logs). A missing/misnamed variable is the usual cause —
  compare against `.env.example`.
- **Webhook returns 401:** the `?key=` in the URL doesn't match `WEBHOOK_KEY` in
  Railway. Re-copy it exactly.
- **Webhook does nothing:** confirm the workflow is ON and the URL path is
  `/webhook`. Check Deploy Logs for the incoming request.
- **Scores look thin / all conservative:** most often the `sales-email-read`
  scope is missing (emails 403 — you'll see a `[hubspot_client] WARNING` in the
  logs), or the specific ticket genuinely has no associated communications.
  The code now logs a stderr WARNING on any non-200 fetch, so watch the logs.
- **High-volume tickets:** batch reads are chunked to 100 inputs, so tickets
  with >100 engagements of one type are handled (previously they 400'd).
- **Costs higher than expected:** keep `ANTHROPIC_MODEL`/`OPENAI_MODEL` on the
  small model, and make sure the sideways detector enrolls on real customer
  replies, not every internal note.
