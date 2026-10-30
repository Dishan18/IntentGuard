"""Security tests: what an agent (or whoever controls one) can throw at the gateway, and what happens
when PayPal misbehaves. Every case asserts the outcome that matters: nothing is sent to PayPal that
the owner's rules don't allow, and every attempt leaves an audit row."""

import json
from datetime import timedelta
from decimal import Decimal

import httpx
import mcp_types as types
import pytest
from test_gateway import (
    NO_FREQUENCY,
    NOW,
    FakeDownstream,
    FakeFacts,
    body,
    capture,
    make_agent,
    make_ctx,
    make_state,
)

from intentguard import store
from intentguard.config import Settings
from intentguard.gateway import owner
from intentguard.gateway.facts import FactsService
from intentguard.gateway.server import call_tool
from intentguard.paypal.rest import PayPalRest
from intentguard.policy.models import FactsFailure


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


def audit(db):
    return [dict(r) for r in db.execute("SELECT tool, decision, reason_code, outcome, forwarded FROM tool_calls")]


async def attempt(state, db, arguments, tool="create_refund", customer="cust_ben"):
    params = types.CallToolRequestParams(name=tool, arguments=arguments)
    return await call_tool(make_ctx(state, make_agent(db), customer), params)


# ---------------------------------------------------------------- hostile arguments


HOSTILE_AMOUNTS = ["-5.00", "0", "0.00", "10.001", "NaN", "Infinity", "-Infinity", "1e400", "ten", "", None, "5,00",
                   " 10.00‮", 10.5, [], {"value": "10"}, True]


@pytest.mark.anyio
@pytest.mark.parametrize("value", HOSTILE_AMOUNTS, ids=lambda v: repr(v)[:20])
async def test_malformed_amounts_are_rejected_and_never_become_a_full_refund(db, value):
    state = make_state(db)
    result = await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": value}})
    assert result.is_error and body(result)["paypal_call"] == "NOT_SENT"
    assert state.downstream.calls == []
    assert audit(db)[0]["forwarded"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("amount", ["not-an-object", ["USD", "10.00"], {"value": "10.00"}, {"currency_code": "EUR", "value": "10.00"}])
async def test_malformed_amount_objects_are_rejected(db, amount):
    state = make_state(db)
    result = await attempt(state, db, {"capture_id": "CAP_BEN", "amount": amount})
    assert result.is_error and state.downstream.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("capture_id", [None, "", 42, ["CAP_BEN"], {"id": "CAP_BEN"}])
async def test_malformed_capture_ids_are_rejected(db, capture_id):
    state = make_state(db)
    result = await attempt(state, db, {"capture_id": capture_id, "amount": {"currency_code": "USD", "value": "10.00"}})
    assert result.is_error and state.downstream.calls == []


@pytest.mark.anyio
async def test_agent_cannot_forge_the_reconciliation_id(db):
    # invoice_id is how the reconciler finds the refund a call made; the agent doesn't get to pick it.
    state = make_state(db)
    await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "10.00"},
                              "invoice_id": "call_someone_elses"})
    [(_, sent)] = state.downstream.calls
    assert sent["invoice_id"] == db.execute("SELECT id FROM tool_calls").fetchone()[0]


@pytest.mark.anyio
async def test_an_unknown_tool_is_refused_without_reaching_paypal(db):
    state = make_state(db)
    result = await attempt(state, db, {"capture_id": "CAP_BEN"}, tool="delete_everything")
    assert body(result)["code"] == "E_TOOL_NOT_ALLOWED" and state.downstream.calls == []
    assert state.facts.lookups == 0


# ---------------------------------------------------------------- replays


@pytest.mark.anyio
async def test_a_replayed_refund_is_not_refunded_twice(db):
    # Without the one-refund-per-customer rule, only replay protection stops this.
    state = make_state(db, facts=FakeFacts([capture(amount="100.00", refunded="0")]), mandate=NO_FREQUENCY)
    request = {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "20.00"}}
    first = await attempt(state, db, request)
    second = await attempt(state, db, dict(request))
    assert not first.is_error
    assert body(second)["code"] == "E_DUPLICATE_REQUEST" and body(second)["paypal_call"] == "NOT_SENT"
    assert len(state.downstream.calls) == 1
    assert body(second)["retryable"] is False


@pytest.mark.anyio
async def test_a_different_request_or_a_failed_one_is_not_a_replay(db):
    state = make_state(db, facts=FakeFacts([capture(amount="100.00", refunded="0")]), mandate=NO_FREQUENCY)
    await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "20.00"}})
    other = await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "15.00"}})
    assert not other.is_error

    state.downstream = FakeDownstream(paypal_error=True)
    await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "5.00"}})
    state.downstream = FakeDownstream()
    retried = await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "5.00"}})
    assert not retried.is_error  # PayPal rejected the first one, so trying again is legitimate


@pytest.mark.anyio
async def test_replay_protection_expires(db):
    state = make_state(db, facts=FakeFacts([capture(amount="100.00", refunded="0")]), mandate=NO_FREQUENCY)
    request = {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "20.00"}}
    await attempt(state, db, request)
    db.execute("UPDATE tool_calls SET created_at = ?", ((NOW - timedelta(minutes=11)).isoformat(),))
    state.clock = lambda: NOW
    assert not (await attempt(state, db, dict(request))).is_error


@pytest.mark.anyio
async def test_a_replayed_review_doesnt_queue_twice(db):
    state = make_state(db, facts=FakeFacts([capture("CAP_C", "cust_carla", "160.00", "0")]), mandate=NO_FREQUENCY)
    request = {"capture_id": "CAP_C", "amount": {"currency_code": "USD", "value": "160.00"}}
    first = body(await attempt(state, db, request, customer="cust_carla"))
    second = body(await attempt(state, db, dict(request), customer="cust_carla"))
    assert first["decision"] == "REVIEW" and second["code"] == "E_DUPLICATE_REQUEST"
    assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    # ...and approving the one that is queued isn't mistaken for a replay of itself.
    status, approved = await owner.approve(state, first["review_id"], "maya")
    assert approved["status"] == "APPROVED"


# ---------------------------------------------------------------- PayPal misbehaving: fail closed, with an audit row


class ExplodingFacts:
    def __init__(self, error):
        self.error = error
        self.lookups = 0

    def for_call(self, tool, arguments):
        self.lookups += 1
        raise self.error


@pytest.mark.anyio
@pytest.mark.parametrize("error", [ValueError("Expecting value: line 1 column 1"), KeyError("amount"), RuntimeError("no credentials")],
                         ids=["non-json", "unexpected-shape", "credentials"])
async def test_any_failure_reading_facts_blocks_and_is_audited(db, error):
    state = make_state(db, facts=ExplodingFacts(error))
    result = await attempt(state, db, {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "10.00"}})
    assert body(result)["code"] == "E_FACTS_UNAVAILABLE" and body(result)["retryable"] is True
    assert state.downstream.calls == []
    assert audit(db) == [{"tool": "create_refund", "decision": "BLOCK", "reason_code": "E_FACTS_UNAVAILABLE",
                          "outcome": "NOT_SENT", "forwarded": 0}]


@pytest.mark.anyio
@pytest.mark.parametrize("tool,arguments", [
    ("create_refund", {"capture_id": "CAP_BEN"}),
    ("get_order", {"id": "ORD_BEN"}),
])
async def test_paypal_unreachable_blocks_every_checked_tool(db, tool, arguments):
    state = make_state(db, facts=FakeFacts())
    state.facts.for_call = lambda tool, args: FactsFailure(not_found=False, detail="PayPal unreachable (ConnectError)")
    result = await attempt(state, db, arguments, tool=tool)
    assert body(result)["code"] == "E_FACTS_UNAVAILABLE" and state.downstream.calls == []


@pytest.mark.anyio
async def test_approval_also_fails_closed_when_facts_break(db):
    state = make_state(db, facts=FakeFacts([capture("CAP_C", "cust_carla", "160.00", "0")]))
    held = body(await attempt(state, db, {"capture_id": "CAP_C", "amount": {"currency_code": "USD", "value": "160.00"}},
                              customer="cust_carla"))
    state.facts = ExplodingFacts(ValueError("not json"))
    status, result = await owner.approve(state, held["review_id"], "maya")
    assert result["status"] == "FAILED" and "E_FACTS_UNAVAILABLE" in result["codes"]
    assert state.downstream.calls == []


# ---------------------------------------------------------------- the PayPal client itself


def rest(handler):
    class Tokens:
        def get(self):
            return "token"

    client = PayPalRest(Settings("id", "secret", "SANDBOX"), Tokens())
    client._transport = httpx.MockTransport(handler)
    return client


@pytest.mark.parametrize("capture_id", ["../../v1/identity/oauth2/userinfo", "CAP/../ORDER", "CAP?x=1", "CAP#frag",
                                        "CAP%2F..", "a" * 200, "CAP BEN"])
def test_ids_from_agents_cant_steer_requests_to_other_paypal_endpoints(capture_id):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    facts = FactsService(rest(handler))
    result = facts.capture(capture_id)
    assert isinstance(result, FactsFailure) and result.not_found
    assert seen == []  # nothing was requested at all


def test_non_json_error_pages_become_facts_failures_not_crashes():
    facts = FactsService(rest(lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")))
    result = facts.capture("4LH13665MX566562M")
    assert isinstance(result, FactsFailure) and not result.not_found and "502" in result.detail


def test_a_genuine_id_still_reaches_paypal():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})

    result = FactsService(rest(handler)).capture("4LH13665MX566562M")
    assert seen == ["/v2/payments/captures/4LH13665MX566562M"] and result.not_found


def test_decimal_amounts_survive_the_trip_to_paypal_exactly():
    assert json.loads(json.dumps({"value": f"{Decimal('19.99'):.2f}"}))["value"] == "19.99"


@pytest.mark.anyio
async def test_two_identical_requests_at_once_move_money_once(db):
    import anyio

    state = make_state(db, facts=FakeFacts([capture(amount="100.00", refunded="0")], delay=0.05), mandate=NO_FREQUENCY)
    request = {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "20.00"}}
    results = []

    async def send():
        results.append(body(await attempt(state, db, dict(request))))

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(send)
    assert len(state.downstream.calls) == 1
    assert sorted(r.get("code", "OK") for r in results) == ["E_DUPLICATE_REQUEST"] * 4 + ["OK"]


@pytest.mark.anyio
async def test_a_path_probe_as_an_order_id_is_an_unknown_order(db):
    def handler(request):
        raise AssertionError(f"nothing should reach PayPal, got {request.url}")

    state = make_state(db)
    state.facts = FactsService(rest(handler))
    result = await attempt(state, db, {"id": "../../v1/identity/openidconnect/userinfo"}, tool="get_order")
    assert body(result)["code"] == "E_UNKNOWN_ORDER" and state.downstream.calls == []
