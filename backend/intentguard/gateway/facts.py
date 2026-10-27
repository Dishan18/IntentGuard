"""Facts the policy engine needs, read directly from PayPal's REST API.

The agent's tool arguments are claims; these lookups are how the gateway checks them. They go to
REST, not through the MCP server the agent uses, and they never raise: any problem becomes a
FactsFailure, which the engine treats as a block (fail closed).
"""

from datetime import datetime
from decimal import Decimal

import httpx

from intentguard.paypal.rest import PayPalAPIError, PayPalRest
from intentguard.policy.models import CaptureFacts, Facts, FactsFailure, InvoiceFacts, OrderFacts

# Refunds in these states count against what's left to refund on a capture.
COUNTED_REFUND_STATUSES = {"COMPLETED", "PENDING"}

NOT_FOUND_ISSUES = {"RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID"}


def _link(resource: dict, rel: str) -> str | None:
    return next((link["href"] for link in resource.get("links", []) if link.get("rel") == rel), None)


def _last_segment(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def _failure(error: Exception, what: str) -> FactsFailure:
    if isinstance(error, PayPalAPIError):
        issues = {detail.get("issue") for detail in error.body.get("details", [])}
        not_found = error.status_code == 404 or bool(issues & NOT_FOUND_ISSUES)
        return FactsFailure(not_found=not_found, detail=f"{what}: {error}")
    return FactsFailure(not_found=False, detail=f"{what}: PayPal unreachable ({type(error).__name__})")


class FactsService:
    def __init__(self, paypal: PayPalRest):
        self._paypal = paypal

    def for_call(self, tool: str, arguments: dict) -> Facts:
        """Facts needed to evaluate this tool call, or None if the call needs none."""
        if tool == "create_refund":
            return self.capture(str(arguments.get("capture_id", "")))
        if tool == "get_refund":
            return self.refund(str(arguments.get("refund_id", "")))
        if tool == "get_order":
            return self.order(str(arguments.get("id", "")))
        if tool in ("send_invoice", "send_invoice_reminder"):
            return self.invoice(str(arguments.get("invoice_id", "")))
        return None

    def capture(self, capture_id: str) -> CaptureFacts | FactsFailure:
        if not capture_id:
            return FactsFailure(not_found=True, detail="no capture_id given")
        try:
            capture = self._paypal.get_capture(capture_id)
            order_id = _last_segment(_link(capture, "up"))
            refunds = self._refunds_for_capture(order_id, capture_id) if order_id else []
        except (PayPalAPIError, httpx.HTTPError) as error:
            return _failure(error, f"capture {capture_id}")
        return CaptureFacts(
            capture_id=capture_id,
            order_id=order_id,
            customer_id=capture.get("custom_id"),
            amount=Decimal(capture["amount"]["value"]),
            currency=capture["amount"]["currency_code"],
            already_refunded=sum(
                (Decimal(refund["amount"]["value"]) for refund in refunds if refund["status"] in COUNTED_REFUND_STATUSES),
                Decimal("0"),
            ),
            status=capture["status"],
            captured_at=datetime.fromisoformat(capture["create_time"]),
        )

    def _refunds_for_capture(self, order_id: str, capture_id: str) -> list[dict]:
        order = self._paypal.get_order(order_id)
        return [
            refund
            for unit in order.get("purchase_units", [])
            for refund in unit.get("payments", {}).get("refunds", [])
            if _last_segment(_link(refund, "up")) == capture_id
        ]

    def refund(self, refund_id: str) -> CaptureFacts | FactsFailure:
        if not refund_id:
            return FactsFailure(not_found=True, detail="no refund_id given")
        try:
            refund = self._paypal.get_refund(refund_id)
        except (PayPalAPIError, httpx.HTTPError) as error:
            return _failure(error, f"refund {refund_id}")
        capture_id = _last_segment(_link(refund, "up"))
        if not capture_id:
            return FactsFailure(not_found=False, detail=f"refund {refund_id} has no capture link")
        return self.capture(capture_id)

    def order(self, order_id: str) -> OrderFacts | FactsFailure:
        if not order_id:
            return FactsFailure(not_found=True, detail="no order id given")
        try:
            order = self._paypal.get_order(order_id)
        except (PayPalAPIError, httpx.HTTPError) as error:
            return _failure(error, f"order {order_id}")
        units = order.get("purchase_units") or [{}]
        return OrderFacts(order_id=order_id, customer_id=units[0].get("custom_id"))

    def invoice(self, invoice_id: str) -> InvoiceFacts | FactsFailure:
        if not invoice_id:
            return FactsFailure(not_found=True, detail="no invoice_id given")
        try:
            invoice = self._paypal.get_invoice(invoice_id)
        except (PayPalAPIError, httpx.HTTPError) as error:
            return _failure(error, f"invoice {invoice_id}")
        amount = invoice.get("amount") or {}
        return InvoiceFacts(
            invoice_id=invoice_id,
            status=invoice.get("status", "UNKNOWN"),
            recipient_emails=tuple(
                email
                for recipient in invoice.get("primary_recipients", [])
                if (email := (recipient.get("billing_info") or {}).get("email_address"))
            ),
            total=Decimal(amount.get("value", "0")),
            currency=amount.get("currency_code", ""),
        )
