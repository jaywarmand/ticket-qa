"""
Core scoring pipeline + two entrypoints.

CLI (manual / testing):
    python score_ticket.py <ticket_id>

Webhook server (workflow trigger):
    python score_ticket.py --serve
    # Point each HubSpot workflow at the same host, using ?mode= to pick logic:
    #   POST /webhook              -> retrospective QA   (enroll on Closed)
    #   POST /webhook?mode=closure -> closure gate        (enroll on Resolved)
    #   POST /webhook?mode=risk    -> sideways detector   (enroll on Customer Responded)

The webhook path is protected by verifying HubSpot's request signature so
random callers can't run your model budget.
"""

import os
import sys
import json
import hashlib
import hmac
import base64

import hubspot_client as hs
from llm import score_ticket as run_model
import live_monitor


def score_one(ticket_id, dry_run=False):
    props = hs.get_ticket(ticket_id)
    engagements = hs.collect_engagements(ticket_id)
    transcript = hs.build_transcript(props, engagements)
    scores = run_model(transcript)
    if not dry_run:
        hs.write_scores(ticket_id, scores)
    return scores, len(engagements)


# ---------- webhook server ----------
# Only import Flask if serving, so the CLI path has no extra dependency.
def _make_app():
    from flask import Flask, request, abort
    app = Flask(__name__)
    # Service Keys don't provide a webhook signing secret, so we authenticate
    # the webhook with a shared key carried in the URL (?key=...). Set WEBHOOK_KEY
    # to a long random string and include the same value in each HubSpot workflow
    # webhook URL. Requests without the correct key are rejected.
    WEBHOOK_KEY = os.environ.get("WEBHOOK_KEY", "")

    # Optional: still supported if you ever move to an app with a signing secret.
    CLIENT_SECRET = os.environ.get("HUBSPOT_CLIENT_SECRET", "")

    # Stage filtering is normally done by the HubSpot workflow; left blank here.
    TRIGGER_STAGE = os.environ.get("TRIGGER_STAGE_ID", "")

    def _verify(req):
        """Authenticate the webhook.

        Primary path (Service Keys): compare ?key= against WEBHOOK_KEY.
        Legacy path: if HUBSPOT_CLIENT_SECRET is set, verify HubSpot's v3
        signature instead. If neither is configured, allow (dev only).
        """
        if WEBHOOK_KEY:
            supplied = req.args.get("key", "")
            return hmac.compare_digest(supplied, WEBHOOK_KEY)
        if CLIENT_SECRET:
            sig = req.headers.get("X-HubSpot-Signature-v3", "")
            ts = req.headers.get("X-HubSpot-Request-Timestamp", "")
            base_string = req.method + req.url + req.get_data(as_text=True) + ts
            digest = hmac.new(CLIENT_SECRET.encode(), base_string.encode(),
                              hashlib.sha256).digest()
            expected = base64.b64encode(digest).decode()
            return hmac.compare_digest(expected, sig)
        return True  # nothing configured — dev only

    def _dispatch(mode, tid):
        """Route to the right logic based on ?mode= on the webhook URL.
          (default)  -> retrospective QA scoring (after close)
          closure    -> premature-closure gate (pre-close)
          risk       -> sideways / at-risk detector (open ticket)
        """
        if mode == "closure":
            out = live_monitor.run_closure_gate(tid)
            return {"mode": "closure", "safe_to_close": out.get("safe_to_close")}
        if mode == "risk":
            out = live_monitor.run_risk(tid)
            return {"mode": "risk", "risk_score": out.get("risk_score"),
                    "flag": out.get("risk_flag")}
        scores, n = score_one(tid)
        return {"mode": "qa", "engagements": n}

    @app.route("/webhook", methods=["POST"])
    def webhook():
        if not _verify(request):
            abort(401)
        mode = request.args.get("mode", "qa").lower()
        if mode not in ("qa", "closure", "risk"):
            mode = "qa"
        events = request.get_json(force=True, silent=True) or []
        results = []
        for ev in events:
            # HubSpot property-change events carry objectId + propertyValue.
            # Stage filtering is normally done by the HubSpot workflow, so
            # TRIGGER_STAGE is optional and left blank in the multi-workflow setup.
            if TRIGGER_STAGE and str(ev.get("propertyValue")) != TRIGGER_STAGE:
                continue
            tid = ev.get("objectId")
            if not tid:
                continue
            try:
                info = _dispatch(mode, tid)
                results.append({"ticket": tid, "ok": True, **info})
            except Exception as e:
                results.append({"ticket": tid, "ok": False, "mode": mode,
                                "error": str(e)})
        return {"processed": results}, 200

    @app.route("/health")
    def health():
        return {"ok": True, "provider": os.environ.get("PROVIDER", "anthropic"),
                "modes": ["qa", "closure", "risk"]}

    return app


def main():
    if len(sys.argv) < 2:
        print("usage: python score_ticket.py <ticket_id> | --serve")
        sys.exit(1)
    if sys.argv[1] == "--serve":
        app = _make_app()
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
        return
    dry = "--dry-run" in sys.argv
    scores, n = score_one(sys.argv[1], dry_run=dry)
    print(f"Engagements analyzed: {n}")
    print(json.dumps(scores, indent=2))
    if dry:
        print("(dry-run: nothing written to HubSpot)")


if __name__ == "__main__":
    main()
