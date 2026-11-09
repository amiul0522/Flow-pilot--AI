"""FlowPilot agent: Planner (OpenAI) -> Policy guardrail -> proposal for human approval."""
import json

from openai import AsyncOpenAI

from .config import settings
from .db import now

POLICY = {
    "max_discount_pct": 10,
    "max_installments": 4,
    "max_reminders": 3,
    "min_days_between_contact": 3,
}

SYSTEM_PROMPT = """You are FlowPilot, an accounts-receivable recovery agent for a small business.
Given one overdue invoice and the client's payment history, choose ONE action:
- "remind": send a payment reminder through PayPal
- "payment_plan": offer installments (optionally a small discount) with a PayPal payment link
- "escalate": hand to the owner (use when the client looks unresponsive or high-risk)
- "wait": do nothing yet
Reply with JSON only:
{"action": "...", "tone": "friendly|firm|final", "discount_pct": 0-100,
 "installments": 1-6, "message": "<message to the client, <=80 words>",
 "reasoning": "<2 sentences for the business owner>"}
Be proportionate: reliable clients get gentle reminders; chronic late payers get plans or escalation."""


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.openai_api_key)


def _heuristic(inv: dict) -> dict:
    """Fallback so the demo still runs without an OpenAI key."""
    late, rate = inv["days_overdue"], inv["history"]["on_time_rate"]
    if rate >= 0.7 and late <= 14:
        action, tone, disc, n = "remind", "friendly", 0, 1
    elif inv["amount"] >= 1000 and late <= 45:
        action, tone, disc, n = "payment_plan", "firm", 5, 3
    else:
        action, tone, disc, n = "escalate", "final", 0, 1
    return {
        "action": action, "tone": tone, "discount_pct": disc, "installments": n,
        "message": (f"Hi {inv['client_name'].split()[0]}, a quick note that invoice for "
                    f"${inv['amount']:.2f} is {late} days overdue. Could you settle it this week?"),
        "reasoning": f"Rule-based fallback: on-time rate {rate:.0%}, {late} days late.",
    }


def apply_policy(inv: dict, p: dict) -> tuple[dict, list[str]]:
    """Hard guardrails. The LLM proposes; policy decides what is allowed."""
    notes: list[str] = []
    p = dict(p)
    if p["action"] in ("remind", "payment_plan") and inv["last_contact_days_ago"] < POLICY["min_days_between_contact"]:
        notes.append(f"Contacted {inv['last_contact_days_ago']}d ago (< {POLICY['min_days_between_contact']}d): action changed to wait")
        p["action"] = "wait"
    if p["action"] in ("remind", "payment_plan") and inv["reminders_sent"] >= POLICY["max_reminders"]:
        notes.append(f"{inv['reminders_sent']} reminders already sent (max {POLICY['max_reminders']}): action changed to escalate")
        p["action"] = "escalate"
    if p.get("discount_pct", 0) > POLICY["max_discount_pct"]:
        notes.append(f"Discount {p['discount_pct']}% capped at {POLICY['max_discount_pct']}%")
        p["discount_pct"] = POLICY["max_discount_pct"]
    if p.get("installments", 1) > POLICY["max_installments"]:
        notes.append(f"Installments {p['installments']} capped at {POLICY['max_installments']}")
        p["installments"] = POLICY["max_installments"]
    p["discount_pct"] = max(0, p.get("discount_pct", 0))
    p["installments"] = max(1, p.get("installments", 1))
    return p, notes


async def propose(inv: dict) -> dict:
    trace = [{"ts": now(), "step": "observe", "detail":
              f"{inv['client_name']} owes ${inv['amount']:.2f}, {inv['days_overdue']}d overdue, "
              f"{inv['reminders_sent']} reminders sent, on-time rate {inv['history']['on_time_rate']:.0%}"}]

    if settings.openai_api_key:
        try:
            ctx = {k: inv[k] for k in ("client_name", "amount", "currency", "description", "days_overdue",
                                       "reminders_sent", "last_contact_days_ago", "history")}
            resp = await _client().chat.completions.create(
                model=settings.openai_model,
                response_format={"type": "json_object"},
                temperature=0.3,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": json.dumps(ctx)}],
            )
            raw = json.loads(resp.choices[0].message.content)
            source = f"openai:{settings.openai_model}"
        except Exception as e:  # network/quota/JSON errors -> fall back, keep the demo alive
            raw, source = _heuristic(inv), f"fallback ({type(e).__name__})"
    else:
        raw, source = _heuristic(inv), "fallback (no OPENAI_API_KEY)"

    raw.setdefault("action", "remind")
    raw.setdefault("discount_pct", 0)
    raw.setdefault("installments", 1)
    trace.append({"ts": now(), "step": "plan", "detail": f"[{source}] {raw['action']} / {raw.get('tone', '-')}: {raw.get('reasoning', '')}"})

    final, notes = apply_policy(inv, raw)
    for n in notes:
        trace.append({"ts": now(), "step": "policy", "detail": n})
    if not notes:
        trace.append({"ts": now(), "step": "policy", "detail": "All guardrails passed"})

    return {"proposal": final, "trace": trace, "source": source}
