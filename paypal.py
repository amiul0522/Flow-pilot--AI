"""Thin async PayPal REST client (sandbox by default).

Docs: https://developer.paypal.com/docs/api/invoicing/v2/ and /orders/v2/
"""
import time
import uuid

import httpx

from .config import settings


class PayPalError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"PayPal {status}: {body}")


class PayPalClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at = 0.0
        self._http = httpx.AsyncClient(base_url=settings.paypal_base_url, timeout=30)

    # ---------- auth ----------
    async def _get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        if not (settings.paypal_client_id and settings.paypal_client_secret):
            raise PayPalError(500, "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set")
        r = await self._http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.paypal_client_id, settings.paypal_client_secret),
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            raise PayPalError(r.status_code, r.text)
        data = r.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 300))
        return self._token

    async def _request(self, method: str, path: str, json: dict | None = None,
                       params: dict | None = None, request_id: str | None = None) -> dict:
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id or uuid.uuid4().hex,
        }
        r = await self._http.request(method, path, json=json, params=params, headers=headers)
        if r.status_code >= 400:
            raise PayPalError(r.status_code, r.text)
        return r.json() if r.content else {}

    # ---------- health ----------
    async def health(self) -> dict:
        await self._get_token()
        return {"ok": True, "base_url": settings.paypal_base_url}

    # ---------- invoicing v2 ----------
    async def create_invoice(self, *, client_name: str, email: str, description: str,
                             amount: float, currency: str = "USD", note: str = "") -> str:
        """Creates a DRAFT invoice and returns its PayPal invoice id (INV2-XXXX-...)."""
        given, _, surname = client_name.partition(" ")
        body = {
            "detail": {
                "invoice_number": f"FP-{uuid.uuid4().hex[:8].upper()}",
                "currency_code": currency,
                "note": note or "Created by FlowPilot AI",
                "payment_term": {"term_type": "NET_10"},
            },
            "primary_recipients": [{
                "billing_info": {
                    "name": {"given_name": given, "surname": surname or "."},
                    "email_address": email,
                }
            }],
            "items": [{
                "name": description[:200],
                "quantity": "1",
                "unit_amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            }],
        }
        res = await self._request("POST", "/v2/invoicing/invoices", json=body)
        # Response: {"rel": "self", "href": ".../v2/invoicing/invoices/INV2-...", "method": "GET"}
        return res["href"].rstrip("/").split("/")[-1]

    async def send_invoice(self, invoice_id: str) -> dict:
        return await self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_recipient": True, "send_to_invoicer": False},
        )

    async def get_invoice(self, invoice_id: str) -> dict:
        return await self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")

    async def remind_invoice(self, invoice_id: str, subject: str, note: str) -> dict:
        return await self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
            json={"subject": subject[:100], "note": note[:4000], "send_to_invoicer": False},
        )

    # ---------- orders v2 (payment links for installments) ----------
    async def create_payment_link(self, *, amount: float, description: str, reference: str,
                                  return_url: str, currency: str = "USD") -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference[:256],
                "description": description[:127],
                "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "FlowPilot AI",
                "user_action": "PAY_NOW",
                "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                "return_url": return_url,
                "cancel_url": return_url,
            }}},
        }
        res = await self._request("POST", "/v2/checkout/orders", json=body)
        link = next((l["href"] for l in res.get("links", [])
                     if l.get("rel") in ("payer-action", "approve")), None)
        return {"order_id": res.get("id"), "status": res.get("status"), "payment_link": link}


paypal = PayPalClient()
