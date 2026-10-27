from decimal import Decimal

import httpx

from intentguard.gateway.facts import FactsService
from intentguard.paypal.rest import PayPalAPIError
from intentguard.policy.models import CaptureFacts, FactsFailure, InvoiceFacts, OrderFacts

API = "https://api-m.sandbox.paypal.com/v2"


def link(rel, path):
    return {"rel": rel, "href": f"{API}{path}"}


CAPTURE = {
    "id": "CAP1",
    "status": "PARTIALLY_REFUNDED",
    "custom_id": "cust_ben",
    "amount": {"currency_code": "USD", "value": "42.00"},
    "create_time": "2026-10-27T11:18:46Z",
    "links": [link("self", "/payments/captures/CAP1"), link("up", "/checkout/orders/ORD1")],
}


def refund(refund_id, value, status, capture_id="CAP1"):
    return {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": value},
        "links": [link("self", f"/payments/refunds/{refund_id}"), link("up", f"/payments/captures/{capture_id}")],
    }


ORDER = {
    "id": "ORD1",
    "purchase_units": [
        {
            "custom_id": "cust_ben",
            "payments": {
                "refunds": [
                    refund("R1", "5.00", "COMPLETED"),
                    refund("R2", "3.00", "PENDING"),
                    refund("R3", "20.00", "FAILED"),
                    refund("R4", "9.00", "COMPLETED", capture_id="OTHER_CAPTURE"),
                ]
            },
        }
    ],
}


class FakeRest:
    def __init__(self, error=None):
        self.error = error

    def _maybe_fail(self):
        if self.error:
            raise self.error

    def get_capture(self, capture_id):
        self._maybe_fail()
        return CAPTURE

    def get_order(self, order_id):
        self._maybe_fail()
        return ORDER

    def get_refund(self, refund_id):
        self._maybe_fail()
        return refund(refund_id, "5.00", "COMPLETED")

    def get_invoice(self, invoice_id):
        self._maybe_fail()
        return {
            "id": invoice_id,
            "status": "DRAFT",
            "primary_recipients": [{"billing_info": {"email_address": "ana.lima@example.com"}}, {"billing_info": {}}],
            "amount": {"currency_code": "USD", "value": "1500.00"},
        }


def test_capture_facts_count_only_this_captures_live_refunds():
    facts = FactsService(FakeRest()).capture("CAP1")
    assert isinstance(facts, CaptureFacts)
    assert facts.customer_id == "cust_ben"
    assert facts.order_id == "ORD1"
    assert facts.already_refunded == Decimal("8.00")  # R1 + R2; failed R3 and other capture's R4 excluded
    assert facts.refundable == Decimal("34.00")
    assert facts.captured_at.isoformat() == "2026-10-27T11:18:46+00:00"


def test_refund_lookup_resolves_to_its_capture():
    facts = FactsService(FakeRest()).refund("R1")
    assert isinstance(facts, CaptureFacts) and facts.capture_id == "CAP1"


def test_order_facts():
    assert FactsService(FakeRest()).order("ORD1") == OrderFacts("ORD1", "cust_ben")


def test_invoice_facts_skip_recipients_without_email():
    facts = FactsService(FakeRest()).invoice("INV2-1")
    assert isinstance(facts, InvoiceFacts)
    assert facts.recipient_emails == ("ana.lima@example.com",)
    assert facts.total == Decimal("1500.00")


def test_404_is_not_found():
    facts = FactsService(FakeRest(PayPalAPIError(404, {"name": "RESOURCE_NOT_FOUND"}))).capture("NOPE")
    assert facts == FactsFailure(not_found=True, detail=facts.detail)


def test_invalid_id_issue_is_not_found():
    error = PayPalAPIError(400, {"details": [{"issue": "INVALID_RESOURCE_ID"}]})
    assert FactsService(FakeRest(error)).order("bad").not_found


def test_server_error_and_network_error_are_unavailable():
    for error in (PayPalAPIError(500, {"name": "INTERNAL_SERVER_ERROR"}), httpx.ConnectError("down")):
        facts = FactsService(FakeRest(error)).capture("CAP1")
        assert isinstance(facts, FactsFailure) and not facts.not_found


def test_empty_ids_never_call_paypal():
    service = FactsService(FakeRest(AssertionError("should not be called")))
    assert service.capture("").not_found
    assert service.for_call("create_refund", {}).not_found


def test_tools_without_facts_return_none():
    assert FactsService(FakeRest()).for_call("list_invoices", {}) is None
