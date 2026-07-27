# Deploying to Railway — step by step (first-time friendly)

This walks you from "files on your computer" to "HubSpot is calling your live
service." No prior Railway experience assumed. Expect ~1–2 hours the first time,
most of it one-time learning.

There are two ways to get your code into Railway. **Option A (GitHub)** is
recommended — every future change redeploys automatically when you push. Option B
(CLI upload) is a fallback if you'd rather not use GitHub.

---

## Before you start — gather your secrets
You'll paste these into Railway later. Have them ready (see `.env.example`):
- HubSpot Private App **access token** (`HUBSPOT_TOKEN`)
- HubSpot Private App **client secret** (`HUBSPOT_CLIENT_SECRET`)
- Your model key — **Anthropic** (`ANTHROPIC_API_KEY`) or **OpenAI** (`OPENAI_API_KEY`)
- Your **pre-close stage id** (`TRIGGER_STAGE_ID`) — ask if you're unsure; it can be looked up

> Create the HubSpot Private App first (Settings → Integrations → Private Apps →
> Create). Scopes: `tickets` read+write, `crm.objects.contacts.read`,
> `conversations.read`. Copy the token and the client secret.

---

## Option A — Deploy from GitHub (recommended)

### 1. Put the code in a GitHub repo
- Create a new **private** repo on GitHub (e.g. `ticket-qa`).
- Upload all the project files to it. Easiest no-terminal way: on the repo page,
  click **Add file → Upload files**, drag in every file from the project folder
  EXCEPT anything secret, then **Commit**.
- The included `.gitignore` already keeps `.env`, PDFs, and build cruft out. Never
  upload a file containing real tokens.

### 2. Create the Railway project
- Go to railway.app → **New Project**.
- Choose **Deploy from GitHub repo**.
- If it's your first time, Railway will ask to connect your GitHub account and
  which repos it can see — grant access to the `ticket-qa` repo.
- Pick the repo. Railway starts building immediately using the included
  `railway.json` and `Procfile`, so it already knows to run the web service.

### 3. Add your environment variables
- Open the service → **Variables** tab → **Raw Editor** (fastest).
- Paste the contents of `.env.example`, then replace the placeholder values with
  your real ones. Delete the provider block you're NOT using.
- **Do not** add a `PORT` variable — Railway sets it automatically and the code
  reads it.
- Save. Railway redeploys with the new variables.

### 4. Give it a public URL
- Service → **Settings** → **Networking** → **Generate Domain**.
- You'll get something like `https://ticket-qa-production.up.railway.app`.
- Copy it — this is your service's base URL.

### 5. Confirm it's alive (before touching HubSpot)
- In your browser, visit `https://<your-domain>/health`.
- You should see: `{"ok": true, "provider": "anthropic"}` (or `openai`).
- If you see that, the hard part is done. If not, see Troubleshooting below.

---

## Option B — Deploy without GitHub (CLI upload)
Only if you'd rather skip GitHub. Requires installing the Railway CLI.
```
npm i -g @railway/cli      # install the CLI
railway login              # opens browser to log in
cd ticket-qa               # the project folder
railway init               # create a new project (follow prompts)
railway up                 # upload + deploy this folder
```
Then set variables and generate a domain in the Railway dashboard exactly as in
Option A steps 3–5. Future updates mean re-running `railway up`.

---

## Connect HubSpot to your live service
Only after `/health` works.

All three modes share ONE service. The `?mode=` on the URL selects the logic, so
you build three near-identical HD-pipeline workflows that differ only in (a) the
enrollment stage and (b) the URL suffix.

| Workflow | Enroll when ticket status = | Stage ID | Webhook URL |
|---|---|---|---|
| Retrospective QA | Closed | 245698182 | `https://<domain>/webhook?key=YOUR_KEY` |
| Closure gate | Resolved | 245844492 | `https://<domain>/webhook?mode=closure&key=YOUR_KEY` |
| Sideways detector | Customer Responded | 245705643 | `https://<domain>/webhook?mode=risk&key=YOUR_KEY` |

Replace `YOUR_KEY` with the exact value you set for `WEBHOOK_KEY` in Railway.
(Service Keys have no signing secret, so this shared key is what keeps random
callers from running your model bill.)

For each: HubSpot → Automation → **Workflows** → Create → **Ticket-based** →
set the enrollment trigger (add a *Pipeline is HD* filter too) → add action
**Send a webhook** → Method **POST**, the URL from the table → turn it on.

Leave `TRIGGER_STAGE_ID` blank — the workflow does the stage filtering, so the
service processes whatever it's handed. Authentication on the webhook action can
be left as none; the service authenticates via the `?key=` shared secret in the
URL (see `WEBHOOK_KEY`).

Roll these out one at a time (QA first), not all at once — see launch order below.

> The service checks HubSpot's signature on every webhook using
> `HUBSPOT_CLIENT_SECRET`, so random callers can't run up your model bill.

---

## Recommended launch order
1. Deploy, confirm `/health`.
2. Test scoring WITHOUT writing: the service always writes, so for a safe first
   look, use the CLI locally instead — `python score_ticket.py <ticket_id> --dry-run`
   — against 10–15 real tickets and eyeball the output.
3. Once the scores look right, turn on ONE workflow (retrospective QA is simplest).
4. Watch it for a day of real tickets.
5. Add the closure gate (warn-only), then the sideways detector.
6. After you trust the closure gate, set `CLOSURE_BLOCKING=true`.

---

## Troubleshooting
- **`/health` won't load / app keeps restarting:** check the **Deploy Logs**
  (service → Deployments → View Logs). A missing or misnamed variable is the most
  common cause — compare against `.env.example` exactly.
- **Webhook does nothing:** confirm the workflow is ON and the URL ends in
  `/webhook`. Check Deploy Logs for incoming requests. A 401 means the signature
  didn't match — verify `HUBSPOT_CLIENT_SECRET` matches the Private App.
- **Scores look thin / all conservative:** the ticket may lack associated
  conversations. (We checked your portal — associations are healthy — but a
  specific ticket worked entirely by phone with nothing logged would still be
  thin.)
- **"module not found" on build:** ensure `requirements.txt` was uploaded.
- **Costs higher than expected:** confirm `ANTHROPIC_MODEL` / `OPENAI_MODEL` is
  the small model, not a large one, and that the sideways detector isn't
  enrolling on every internal note.
