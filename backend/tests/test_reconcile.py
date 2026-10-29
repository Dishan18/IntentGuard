import copy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from test_gateway import NOW, FakeDownstream, FakeFacts, capture, make_agent, make_ctx, make_state, refund

from intentguard import store
from intentguard.demo import SUPPORT_MANDATE
from intentguard.gateway import webhooks
from intentguard.gateway.ledger import Ledger
from intentguard.gateway.reconcile import UNKNOWN_GRACE, Reconciler
from intentguard.gateway.server import call_tool
from intentguard.paypal.rest import PayPalAPIError

CAPTURE = "CAP_BEN"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


def paypal_refund(refund_id, invoice_id=None, status="COMPLETED", capture_id=CAPTURE):
    body = {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": "20.00"},
        "links": [{"rel": "up", "href": f"https://api.sandbox.paypal.com/v2/payments/captures/{capture_id}"}],
    }
    if invoice_id:
        body["invoice_id"] = invoice_id
    return body


class FakePayPal:
    """PayPal as the reconciler sees it: refunds by ID and per capture, plus signature checks."""

    def __init__(self, *refunds, list_omits_invoice_id=False, down=False):
        self.refunds = {r["id"]: r for r in refunds}
        self.list_omits_invoice_id = list_omits_invoice_id
        self.down = down
        self.reads = 0

    def _check(self):
        self.reads += 1
        if self.down:
            raise PayPalAPIError(503, {"name": "SERVICE_UNAVAILABLE"})

    def get_refund(self, refund_id):
        self._check()
        return copy.deepcopy(self.refunds[refund_id])

    def refunds_for_capture(self, capture_id):
        self._check()
        found = [copy.deepcopy(r) for r in self.refunds.values() if r["links"][0]["href"].endswith(capture_id)]
        if self.list_omits_invoice_id:
            for r in found:
                r.pop("invoice_id", None)
        return found

    def verify_webhook_signature(self, webhook_id, headers, event):
        self._check()
        return webhook_id == "WH-1" and headers.get("paypal-transmission-sig") == "valid"


def reconciler(db, paypal, now=NOW):
    return Reconciler(db, paypal, paypal, lambda: now)


def entry(db, call_id, status, refund_id=None, paypal_status=None, amount="20.00", created=NOW):
    ledger = Ledger(db)
    ledger.reserve(call_id, SUPPORT_MANDATE, "support-agent", "cust_ben", CAPTURE, Decimal(amount), created)
    if status == "COMMITTED":
        ledger.commit(call_id, refund_id, created, paypal_status)
    elif status == "UNKNOWN":
        ledger.mark_unknown(call_id, created)


def ledger_entry(db, call_id):
    return dict(db.execute("SELECT * FROM refund_ledger WHERE call_id = ?", (call_id,)).fetchone())


def spent_today(db):
    return Ledger(db).snapshot(SUPPORT_MANDATE, "cust_ben", NOW).refunded_today


# ---------------------------------------------------------------- UNKNOWN outcomes


def test_unknown_refund_found_by_its_call_id_is_committed(db):
    entry(db, "call_a", "UNKNOWN")
    paypal = FakePayPal(paypal_refund("REF_OTHER", "someone-else"), paypal_refund("REF_A", "call_a"))
    [change] = reconciler(db, paypal).run_once()
    assert change == {"call_id": "call_a", "refund_id": "REF_A", "paypal_status": "COMPLETED", "ledger": "COMMITTED"}
    row = ledger_entry(db, "call_a")
    assert (row["status"], row["refund_id"], row["paypal_status"]) == ("COMMITTED", "REF_A", "COMPLETED")


def test_unknown_match_works_when_the_order_listing_omits_invoice_ids(db):
    entry(db, "call_a", "UNKNOWN")
    paypal = FakePayPal(paypal_refund("REF_A", "call_a"), list_omits_invoice_id=True)
    reconciler(db, paypal).run_once()
    assert ledger_entry(db, "call_a")["refund_id"] == "REF_A"


def test_unknown_without_a_refund_waits_then_is_released(db):
    entry(db, "call_a", "UNKNOWN")
    paypal = FakePayPal(paypal_refund("REF_OTHER", "someone-else"))
    assert reconciler(db, paypal, NOW + timedelta(minutes=2)).run_once() == []
    assert ledger_entry(db, "call_a")["status"] == "UNKNOWN"
    assert spent_today(db) == Decimal("20.00")  # still counted while we can't tell

    later = NOW + UNKNOWN_GRACE + timedelta(seconds=1)
    [change] = reconciler(db, paypal, later).run_once()
    assert change["ledger"] == "RELEASED"
    row = ledger_entry(db, "call_a")
    assert (row["status"], row["paypal_status"]) == ("RELEASED", "NOT_FOUND")
    assert spent_today(db) == Decimal("0")


@pytest.mark.anyio
async def test_gateway_timeout_then_reconcile_end_to_end(db):
    # PayPal received the refund but the gateway never heard back.
    state = make_state(db, downstream=FakeDownstream(error=TimeoutError()), facts=FakeFacts([capture()]))
    with pytest.raises(TimeoutError):
        await call_tool(make_ctx(state, make_agent(db)), refund(value="20.00"))
    [(_, sent)] = state.downstream.calls
    call_id = db.execute("SELECT call_id FROM refund_ledger").fetchone()[0]
    assert sent["invoice_id"] == call_id and ledger_entry(db, call_id)["status"] == "UNKNOWN"

    reconciler(db, FakePayPal(paypal_refund("REF_LATE", sent["invoice_id"]))).run_once()

    row = ledger_entry(db, call_id)
    assert (row["status"], row["refund_id"]) == ("COMMITTED", "REF_LATE")


# ---------------------------------------------------------------- pending refunds


def test_pending_refund_settles_to_completed(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    reconciler(db, FakePayPal(paypal_refund("REF_A", "call_a", "COMPLETED"))).run_once()
    assert ledger_entry(db, "call_a")["paypal_status"] == "COMPLETED"
    assert reconciler(db, FakePayPal()).run_once() == []  # final: not checked again


def test_failed_refund_is_released_and_stops_counting(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    reconciler(db, FakePayPal(paypal_refund("REF_A", "call_a", "FAILED"))).run_once()
    row = ledger_entry(db, "call_a")
    assert (row["status"], row["paypal_status"]) == ("RELEASED", "FAILED")
    assert spent_today(db) == Decimal("0")


def test_still_pending_is_left_alone(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    assert reconciler(db, FakePayPal(paypal_refund("REF_A", "call_a", "PENDING"))).run_once() == []


def test_paypal_errors_dont_stop_the_pass(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    paypal = FakePayPal(down=True)
    assert reconciler(db, paypal).run_once() == []
    assert ledger_entry(db, "call_a")["paypal_status"] == "PENDING"


# ---------------------------------------------------------------- webhooks


def webhook_state(db, paypal, webhook_id="WH-1"):
    return SimpleNamespace(db=db, reconciler=reconciler(db, paypal), webhook_id=webhook_id, clock=lambda: NOW)


def client(state):
    app = Starlette(routes=webhooks.routes(lambda: state))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test")


SIGNED = {"PayPal-Transmission-Sig": "valid", "PayPal-Transmission-Id": "t1", "PayPal-Auth-Algo": "SHA256withRSA"}


def event(event_type="PAYMENT.REFUND.FAILED", resource=None, event_id="WH-EVT-1"):
    return {"id": event_id, "event_type": event_type, "resource": resource or paypal_refund("REF_A", "call_a", "FAILED")}


def processed_events(db):
    return db.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0]


@pytest.mark.anyio
async def test_webhooks_are_refused_until_a_webhook_id_is_configured(db):
    async with client(webhook_state(db, FakePayPal(), webhook_id="")) as http:
        response = await http.post("/webhooks/paypal", json=event(), headers=SIGNED)
    assert response.status_code == 503


@pytest.mark.anyio
async def test_forged_webhook_is_rejected_and_changes_nothing(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "COMPLETED")
    paypal = FakePayPal(paypal_refund("REF_A", "call_a", "FAILED"))
    async with client(webhook_state(db, paypal)) as http:
        response = await http.post("/webhooks/paypal", json=event(), headers={"PayPal-Transmission-Sig": "forged"})
    assert response.status_code == 400
    assert ledger_entry(db, "call_a")["status"] == "COMMITTED" and processed_events(db) == 0


@pytest.mark.anyio
async def test_verified_event_is_reconciled_from_paypal_not_from_the_payload(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    # The payload claims FAILED, but PayPal's API says COMPLETED: the API wins.
    paypal = FakePayPal(paypal_refund("REF_A", "call_a", "COMPLETED"))
    async with client(webhook_state(db, paypal)) as http:
        response = await http.post("/webhooks/paypal", json=event(), headers=SIGNED)
    assert response.status_code == 200 and response.json()["result"]["action"] == "reconciled"
    row = ledger_entry(db, "call_a")
    assert (row["status"], row["paypal_status"]) == ("COMMITTED", "COMPLETED")


@pytest.mark.anyio
async def test_redelivered_event_is_processed_once(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    paypal = FakePayPal(paypal_refund("REF_A", "call_a", "FAILED"))
    async with client(webhook_state(db, paypal)) as http:
        first = await http.post("/webhooks/paypal", json=event(), headers=SIGNED)
        reads_after_first = paypal.reads
        second = await http.post("/webhooks/paypal", json=event(), headers=SIGNED)
    assert first.json()["status"] == "processed" and second.json()["status"] == "duplicate"
    assert paypal.reads == reads_after_first + 1  # only the signature check, no second reconciliation
    assert processed_events(db) == 1 and ledger_entry(db, "call_a")["status"] == "RELEASED"


@pytest.mark.anyio
async def test_capture_event_reconciles_entries_on_that_capture(db):
    entry(db, "call_a", "UNKNOWN")
    capture_resource = {"id": CAPTURE, "status": "PARTIALLY_REFUNDED", "links": [
        {"rel": "up", "href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORD"}]}
    paypal = FakePayPal(paypal_refund("REF_A", "call_a"))
    async with client(webhook_state(db, paypal)) as http:
        response = await http.post(
            "/webhooks/paypal", json=event("PAYMENT.CAPTURE.REFUNDED", capture_resource), headers=SIGNED
        )
    assert response.json()["result"]["changes"][0]["refund_id"] == "REF_A"
    assert ledger_entry(db, "call_a")["status"] == "COMMITTED"


@pytest.mark.anyio
async def test_refund_made_outside_intentguard_is_noted_as_external(db):
    paypal = FakePayPal(paypal_refund("REF_DASHBOARD"))
    resource = paypal_refund("REF_DASHBOARD")
    async with client(webhook_state(db, paypal)) as http:
        response = await http.post("/webhooks/paypal", json=event("PAYMENT.CAPTURE.REFUNDED", resource), headers=SIGNED)
    assert response.json()["result"] == {"action": "external", "refund_id": "REF_DASHBOARD", "capture_id": CAPTURE}


@pytest.mark.anyio
async def test_other_event_types_are_acknowledged_and_ignored(db):
    async with client(webhook_state(db, FakePayPal())) as http:
        response = await http.post(
            "/webhooks/paypal", json=event("CUSTOMER.DISPUTE.CREATED", {"id": "PP-D-1"}), headers=SIGNED
        )
    assert response.json()["result"] == {"action": "ignored"} and processed_events(db) == 1


@pytest.mark.anyio
async def test_processing_failure_returns_500_so_paypal_retries(db):
    entry(db, "call_a", "COMMITTED", "REF_A", "PENDING")
    paypal = FakePayPal(paypal_refund("REF_A", "call_a", "COMPLETED"))
    state = webhook_state(db, paypal)
    original = paypal.get_refund

    def flaky(refund_id):
        raise PayPalAPIError(503, {"name": "SERVICE_UNAVAILABLE"})

    paypal.get_refund = flaky
    async with client(state) as http:
        failed = await http.post("/webhooks/paypal", json=event(), headers=SIGNED)
        paypal.get_refund = original
        retried = await http.post("/webhooks/paypal", json=event(), headers=SIGNED)
    assert failed.status_code == 500 and retried.json()["status"] == "processed"
    assert ledger_entry(db, "call_a")["paypal_status"] == "COMPLETED"


@pytest.mark.anyio
async def test_malformed_bodies_are_rejected(db):
    async with client(webhook_state(db, FakePayPal())) as http:
        assert (await http.post("/webhooks/paypal", content=b"not json", headers=SIGNED)).status_code == 400
        assert (await http.post("/webhooks/paypal", json=["x"], headers=SIGNED)).status_code == 400
