"""Thin PayPal REST client for the calls the gateway makes itself (seeding, fact lookups).

Tool calls an agent asks for go through PayPal's MCP server instead; this client is for
reads the gateway must not delegate to anything the agent can influence.
"""

import uuid
from typing import Any

import httpx

from intentguard.config import Settings
from intentguard.paypal.auth import TokenProvider


class PayPalAPIError(RuntimeError):
    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self.body = body
        issues = ", ".join(detail.get("issue", "?") for detail in body.get("details", []))
        super().__init__(f"PayPal {status_code} {body.get('name', '')}: {issues or body.get('message', '')}")


class PayPalRest:
    def __init__(self, settings: Settings, tokens: TokenProvider):
        self._base = settings.paypal_api_base
        self._tokens = tokens

    def _request(self, method: str, path: str, json: dict | None = None, idempotent: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self._tokens.get()}", "Content-Type": "application/json"}
        if idempotent:
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        response = httpx.request(method, f"{self._base}{path}", headers=headers, json=json, timeout=30)
        body = response.json() if response.content else {}
        if response.is_error:
            raise PayPalAPIError(response.status_code, body)
        return body

    def create_order(self, body: dict) -> dict:
        return self._request("POST", "/v2/checkout/orders", json=body, idempotent=True)

    def capture_order(self, order_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", json={}, idempotent=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def get_capture(self, capture_id: str) -> dict:
        return self._request("GET", f"/v2/payments/captures/{capture_id}")

    def get_refund(self, refund_id: str) -> dict:
        return self._request("GET", f"/v2/payments/refunds/{refund_id}")

    def get_invoice(self, invoice_id: str) -> dict:
        return self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")
