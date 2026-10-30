"""Thin PayPal REST client for the calls the gateway makes itself (seeding, fact lookups).

Tool calls an agent asks for go through PayPal's MCP server instead; this client is for
reads the gateway must not delegate to anything the agent can influence.
"""

import re
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


# PayPal resource IDs (orders, captures, refunds, invoices, webhooks). Anything else never reaches a
# URL: an agent-supplied "ID" like "../../v1/..." must not steer a request to another endpoint.
_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _id(value: str) -> str:
    if not isinstance(value, str) or not _RESOURCE_ID.fullmatch(value):
        raise PayPalAPIError(400, {"name": "INVALID_RESOURCE_ID", "details": [{"issue": "INVALID_RESOURCE_ID"}],
                                   "message": "not a PayPal resource ID"})
    return value


class PayPalRest:
    def __init__(self, settings: Settings, tokens: TokenProvider, transport: httpx.BaseTransport | None = None):
        self._base = settings.paypal_api_base
        self._tokens = tokens
        self._transport = transport

    def _request(self, method: str, path: str, json: dict | None = None, idempotent: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self._tokens.get()}", "Content-Type": "application/json"}
        if idempotent:
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        with httpx.Client(transport=self._transport, timeout=30) as client:
            response = client.request(method, f"{self._base}{path}", headers=headers, json=json)
        try:
            body = response.json() if response.content else {}
        except ValueError:  # an HTML error page from a proxy, say
            body = {"name": "NON_JSON_RESPONSE", "message": response.text[:200]}
            if not response.is_error:
                raise PayPalAPIError(502, body) from None
        if not isinstance(body, dict):
            raise PayPalAPIError(502, {"name": "UNEXPECTED_RESPONSE", "message": str(body)[:200]})
        if response.is_error:
            raise PayPalAPIError(response.status_code, body)
        return body

    def create_order(self, body: dict) -> dict:
        return self._request("POST", "/v2/checkout/orders", json=body, idempotent=True)

    def capture_order(self, order_id: str) -> dict:
        return self._request("POST", f"/v2/checkout/orders/{_id(order_id)}/capture", json={}, idempotent=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{_id(order_id)}")

    def get_capture(self, capture_id: str) -> dict:
        return self._request("GET", f"/v2/payments/captures/{_id(capture_id)}")

    def get_refund(self, refund_id: str) -> dict:
        return self._request("GET", f"/v2/payments/refunds/{_id(refund_id)}")

    def get_invoice(self, invoice_id: str) -> dict:
        return self._request("GET", f"/v2/invoicing/invoices/{_id(invoice_id)}")

    # ------------------------------------------------------------ webhooks

    def verify_webhook_signature(self, webhook_id: str, headers: dict[str, str], event: dict) -> bool:
        """Ask PayPal whether a delivery really came from PayPal for this webhook."""
        body = {
            "auth_algo": headers.get("paypal-auth-algo", ""),
            "cert_url": headers.get("paypal-cert-url", ""),
            "transmission_id": headers.get("paypal-transmission-id", ""),
            "transmission_sig": headers.get("paypal-transmission-sig", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status") == "SUCCESS"

    def list_webhooks(self) -> list[dict]:
        return self._request("GET", "/v1/notifications/webhooks").get("webhooks", [])

    def create_webhook(self, url: str, event_types: list[str]) -> dict:
        body = {"url": url, "event_types": [{"name": name} for name in event_types]}
        return self._request("POST", "/v1/notifications/webhooks", json=body)

    def delete_webhook(self, webhook_id: str) -> None:
        self._request("DELETE", f"/v1/notifications/webhooks/{_id(webhook_id)}")
