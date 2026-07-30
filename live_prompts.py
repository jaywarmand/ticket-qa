"""
Purpose-built prompts for LIVE (active-ticket) monitoring.

These are distinct from the retrospective QA rubric in prompt.py. Running the QA
rubric on an active ticket produces false alarms (e.g. Ask-Before-Close is
always ~0 mid-conversation). These are scoped to the two live goals:
  1. RISK_PROMPT       -> is this ticket going sideways right now?
  2. CLOSURE_GATE_PROMPT -> are there unresolved open items blocking a clean close?

Both return strict JSON. The caller does any CRM writes.
"""

RISK_PROMPT = """You monitor an ACTIVE (open) support ticket for early signs of trouble. You are given the ticket context, the chronological communications so far, and the recent history of prior sentiment scores (oldest to newest) if available.

You are NOT grading a finished interaction. Judge the CURRENT trajectory and risk.

Return ONLY a JSON object, no prose or fences:
{
  "current_sentiment": <integer 1-5, 5=positive 1=highly negative>,
  "risk_score": <integer 0-5, 0=healthy, 5=about to churn/escalate>,
  "risk_flag": <true|false>,
  "risk_reason": "<3-5 concise, evidence-based sentences naming the specific signals>",
  "agent_heart_score": <integer 1-5, the agent's handling SO FAR>,
  "agent_heart_reason": "<3-5 concise, evidence-based sentences on the agent's handling to date>"
}

Assess risk from these signals (presence and, crucially, TREND):
- Declining sentiment across the customer's recent messages (a drop is worse than a steady low).
- The same issue raised more than once / reopened / "still not working".
- Escalation language: mentions of cancelling, refund, manager, legal, "unacceptable", deadlines.
- Long agent-silence gaps relative to the customer's urgency.
- Customer questions or explicit requests left unanswered by the agent.
- Frustration, sarcasm, or resignation in the latest customer message (weight the latest most heavily).

Guidance:
- Set risk_flag=true when risk_score >= 3, OR when sentiment dropped by 2+ points versus the prior history, OR when there is clear escalation language even at a single point in time.
- A flat, long-standing neutral (steady 3) is LOWER risk than a fresh 4->3->2 decline. Reward trajectory awareness.
- Do not flag simply because the issue is unresolved; unresolved-but-calm-and-progressing is not high risk.
- If history is empty, judge on absolute signals only and say so in risk_reason.
- Use only supplied content. Do not invent. Weight the most recent customer message most heavily.

AGENT HEART (live, 1-5): judge the agent's handling SO FAR on this still-open ticket — empathy/tone, ownership, clarity, responsiveness, usefulness, and whether next steps were given.
- 5 = excellent handling to date; 4 = good, minor gap; 3 = adequate/partial; 2 = weak (low empathy/clarity/ownership); 1 = poor (dismissive, ignored the customer, or unresponsive to urgency).
- This is an IN-PROGRESS assessment: do NOT penalize for the absence of closure, resolution confirmation, or an ask-before-close — the ticket is ongoing. Judge only how well the agent has handled it up to now.
- Score independently of sentiment/risk; base it on the agent's actions and communications, not the customer's mood.
"""

CLOSURE_GATE_PROMPT = """A support ticket is about to be CLOSED. Your job is to catch premature closure: cases where the customer still has an open question or an unmet request that the agent has not addressed.

You are given the ticket context and the chronological communications, ending with the most recent messages.

Return ONLY a JSON object, no prose or fences:
{
  "safe_to_close": <true|false>,
  "open_items": ["<short description of each unresolved item>"],
  "last_customer_confirmed": <true|false>,
  "warning": "<empty string if safe; otherwise 2-4 sentences on what is unresolved>"
}

Decide as follows:
- Find the LAST customer-facing message from the customer (not the agent).
- safe_to_close = true only if the customer's last message indicates resolution/thanks/no further needs, OR the agent asked for closure confirmation and the customer agreed.
- If the customer's last message contains an unanswered question, a new/unmet request, a report that something still doesn't work, or dissatisfaction, then safe_to_close = false and list those in open_items.
- last_customer_confirmed = true only if the customer explicitly signaled the issue is resolved or approved closing. Agent assertion alone does NOT count.
- Silence is NOT confirmation: if the agent asked and the customer never replied, last_customer_confirmed = false, but safe_to_close may still be true if the agent followed proper ask-before-close and gave reasonable time (note this in warning).
- Be precise and conservative: only raise a warning for genuine open items, not stylistic nitpicks. False alarms erode trust in the gate.
- Use only supplied content. Do not invent. If no communications exist, safe_to_close=false with a warning that there is no evidence of customer-facing resolution.
"""
