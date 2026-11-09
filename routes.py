import uuid

from fastapi import APIRouter, HTTPException

from .agent import POLICY, propose
from .config import settings
from .db import audit, db, now
from .paypal import PayPalError, paypal

router = APIRouter(prefix="/api")

SEED = [
    ("Maya Chen",     "Brand identity package",      2400.00, 21, 1, 9, {"avg_days_late": 6,  "on_time_rate": 0.80}),
    ("Rohan Mehta",   "Monthly SEO retainer",         1200.00, 38, 2, 6, {"avg_days_late": 19, "on_time_rate": 0.40}),
    ("Lena Fischer",  "Website redesign (phase 2)",   5200.00, 12, 0, 20, {"avg_days_late": 3,  "on_time_rate": 0.90}),
    ("Tom Alvarez",   "Video editing, 6 reels",        640.00, 63, 3, 4, {"avg_days_late": 31, "on_time_rate": 0.20}),
    ("Priya Nair",    "Consulting workshop",          1800.00,  9, 0, 15, {"avg_days_late": 5,  "on_time_rate": 0.75}),
]


def _pp(e: PayPalError) -> HTTPException:
    return HTTPException(502, detail={"paypal_status": e.status, "body": e.body})


async def _get(coll: str, id_: str) -> dict:
    doc = await db[coll].find_one({"id": id_}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"{coll[:-1]} not found")
    return doc


# ---------- health ----------
@router.get("/health")
async def health():
    try:
        await db.command("ping")
        mongo = True
    except Exception:
        mongo = False
    return {"api": True, "mongo": mongo, "openai_configured": bool(settings.openai_api_key),
            "paypal_configured": bool(settings.paypal_client_id and settings.paypal_client_secret)}


@router.get("/paypal/health")
async def paypal_health():
    try:
        return await paypal.health()
    except PayPalError as e:
        raise _pp(e)


# ---------- invoices ----------
@router.post("/seed")
async def seed():
    """Create demo invoices in the PayPal sandbox (draft -> sent) and mirror them in MongoDB."""
    email = settings.demo_recipient_email or "customer@example.com"
    out = []
    for name, desc, amount, late, reminders, last_contact, hist in SEED:
        entry = {"client": name}
        try:
            pid = await paypal.create_invoice(client_name=name, email=email, description=desc, amount=amount)
            entry["paypal_invoice_id"] = pid
            try:
                await paypal.send_invoice(pid)
                status = "SENT"
            except PayPalError as e:
                status = "DRAFT"
                entry["send_error"] = e.body[:300]
        except PayPalError as e:
            raise _pp(e)
        doc = {
            "id": uuid.uuid4().hex[:12], "paypal_invoice_id": pid, "client_name": name,
            "client_email": email, "description": desc, "amount": amount, "currency": "USD",
            # PayPal sandbox invoices are never truly overdue, so the scenario is simulated:
            "days_overdue": late, "reminders_sent": reminders, "last_contact_days_ago": last_contact,
            "history": hist, "status": status, "created_at": now(),
        }
        await db.invoices.insert_one(dict(doc))
        await audit("paypal", "invoice_created", {"invoice": pid, "status": status})
        out.append({**entry, "status": status})
    return {"created": out}


@router.get("/invoices")
async def list_invoices():
    return await db.invoices.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)


@router.post("/invoices/{invoice_id}/sync")
async def sync_invoice(invoice_id: str):
    inv = await _get("invoices", invoice_id)
    try:
        remote = await paypal.get_invoice(inv["paypal_invoice_id"])
    except PayPalError as e:
        raise _pp(e)
    status = remote.get("status", inv["status"])
    await db.invoices.update_one({"id": invoice_id}, {"$set": {"status": status}})
    await audit("paypal", "invoice_synced", {"invoice": inv["paypal_invoice_id"], "status": status})
    return {"id": invoice_id, "status": status}


@router.post("/invoices/{invoice_id}/analyze")
async def analyze(invoice_id: str):
    inv = await _get("invoices", invoice_id)
    result = await propose(inv)
    prop = {
        "id": uuid.uuid4().hex[:12], "invoice_id": invoice_id, "client_name": inv["client_name"],
        "amount": inv["amount"], **result["proposal"], "trace": result["trace"],
        "source": result["source"], "state": "pending", "result": None, "created_at": now(),
    }
    await db.proposals.insert_one(dict(prop))
    await audit("agent", "proposal_created", {"proposal": prop["id"], "action": prop["action"], "source": prop["source"]})
    return prop


# ---------- proposals ----------
@router.get("/proposals")
async def list_proposals():
    return await db.proposals.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)


@router.post("/proposals/{proposal_id}/approve")
async def approve(proposal_id: str):
    p = await _get("proposals", proposal_id)
    if p["state"] != "pending":
        raise HTTPException(409, f"Proposal already {p['state']}")
    inv = await _get("invoices", p["invoice_id"])
    result: dict = {}
    try:
        if p["action"] == "remind":
            await paypal.remind_invoice(inv["paypal_invoice_id"], f"Payment reminder: {inv['description']}", p["message"])
            result = {"sent_via": "paypal_invoice_reminder"}
        elif p["action"] == "payment_plan":
            total = inv["amount"] * (1 - p["discount_pct"] / 100)
            per = round(total / p["installments"], 2)
            link = await paypal.create_payment_link(
                amount=per, description=f"{inv['description']} - installment 1/{p['installments']}",
                reference=f"{inv['id']}-1", return_url=settings.cors_origins[0] + "/?paid=1")
            result = {"installment_amount": per, "installments": p["installments"], **link}
        else:
            result = {"note": f"No PayPal call needed for '{p['action']}'"}
    except PayPalError as e:
        await audit("paypal", "execution_failed", {"proposal": proposal_id, "status": e.status, "body": e.body[:500]})
        raise _pp(e)

    if p["action"] in ("remind", "payment_plan"):
        await db.invoices.update_one({"id": inv["id"]}, {"$inc": {"reminders_sent": 1}, "$set": {"last_contact_days_ago": 0}})
    await db.proposals.update_one({"id": proposal_id}, {"$set": {"state": "executed", "result": result, "decided_at": now()}})
    await audit("human", "proposal_approved", {"proposal": proposal_id, "action": p["action"], "result": result})
    return await _get("proposals", proposal_id)


@router.post("/proposals/{proposal_id}/reject")
async def reject(proposal_id: str):
    p = await _get("proposals", proposal_id)
    if p["state"] != "pending":
        raise HTTPException(409, f"Proposal already {p['state']}")
    await db.proposals.update_one({"id": proposal_id}, {"$set": {"state": "rejected", "decided_at": now()}})
    await audit("human", "proposal_rejected", {"proposal": proposal_id})
    return await _get("proposals", proposal_id)


# ---------- audit + policy ----------
@router.get("/audit")
async def get_audit():
    return await db.audit.find({}, {"_id": 0}).sort("ts", -1).to_list(100)


@router.get("/policy")
async def get_policy():
    return POLICY
