import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import anyio
import mcp_types as types
import pytest

from intentguard import store
from intentguard.demo import SUPPORT_MANDATE
from intentguard.gateway import registry
from intentguard.gateway.downstream import DownstreamUnavailable
from intentguard.gateway.ledger import Ledger
from intentguard.gateway.server import AGENT_SCOPE_KEY, GatewayState, call_tool, list_tools
from intentguard.policy.engine import evaluate
from intentguard.policy.models import AgentView, CaptureFacts, EvaluationInput, Outcome

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# Same as the demo mandate but without the one-refund-per-customer rule, for tests about other limits.
NO_FREQUENCY = SUPPORT_MANDATE.model_copy(
    update={"refunds": SUPPORT_MANDATE.refunds.model_copy(update={"max_refunds_per_customer": None})}
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


def capture(capture_id="CAP_BEN", customer="cust_ben", amount="42.00", refunded="2.00"):
    return CaptureFacts(
        capture_id=capture_id,
        order_id="ORD",
        customer_id=customer,
        amount=Decimal(amount),
        currency="USD",
        already_refunded=Decimal(refunded),
        status="COMPLETED",
        captured_at=NOW - timedelta(days=3),
    )


class FakeFacts:
    """Facts keyed by capture_id; optionally slow, to let concurrent calls interleave."""

    def __init__(self, captures=(), delay=0.0):
        self.captures = {facts.capture_id: facts for facts in captures}
        self.delay = delay
        self.lookups = 0

    def for_call(self, tool, arguments):
        self.lookups += 1
        time.sleep(self.delay)
        return self.captures.get(arguments.get("capture_id"))


class FakeDownstream:
    def __init__(self, error=None, paypal_error=False):
        self.tools = {name: types.Tool(name=name, input_schema={"type": "object"}) for name in ("get_order", "create_refund")}
        self.calls = []
        self._error = error
        self._paypal_error = paypal_error

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._error:
            raise self._error
        if self._paypal_error:
            return types.CallToolResult(content=[types.TextContent(type="text", text="UNPROCESSABLE_ENTITY")], is_error=True)
        body = {"id": f"REF{len(self.calls)}", "status": "COMPLETED"}
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))])


def make_agent(db, tools=("get_order", "create_refund")):
    key = registry.register(db, "support-agent", "Support", "owner", "purpose", list(tools))
    return registry.authenticate(db, key)


def make_state(db, downstream=None, facts=None, mandate=SUPPORT_MANDATE):
    return GatewayState(
        db=db,
        downstream=downstream or FakeDownstream(),
        facts=facts or FakeFacts([capture()]),
        mandates={"support-agent": mandate}.get,
        clock=lambda: NOW,
    )


def make_ctx(state, agent, customer="cust_ben"):
    request = SimpleNamespace(scope={AGENT_SCOPE_KEY: agent}, headers={"x-intentguard-customer": customer})
    return SimpleNamespace(lifespan_context=state, request=request)


def refund(capture_id="CAP_BEN", value=None):
    arguments = {"capture_id": capture_id}
    if value is not None:
        arguments["amount"] = {"currency_code": "USD", "value": value}
    return types.CallToolRequestParams(name="create_refund", arguments=arguments)


def body(result):
    return json.loads(result.content[0].text)


def ledger_rows(db):
    return [dict(row) for row in db.execute("SELECT * FROM refund_ledger ORDER BY created_at")]


# ---------------------------------------------------------------- identity and allowlist


def test_key_is_hashed_and_rekey_revokes_old_key(db):
    first = registry.register(db, "a", "A", "o", "p", ["get_order"])
    stored = db.execute("SELECT key_hash FROM agents").fetchone()["key_hash"]
    assert first not in stored
    second = registry.register(db, "a", "A", "o", "p", ["get_order"])
    assert registry.authenticate(db, first) is None
    assert registry.authenticate(db, second).allowed_tools == frozenset({"get_order"})


@pytest.mark.anyio
async def test_list_tools_filters_to_allowlist(db):
    state = make_state(db)
    result = await list_tools(make_ctx(state, make_agent(db, ["get_order"])), None)
    assert [tool.name for tool in result.tools] == ["get_order"]


@pytest.mark.anyio
async def test_disallowed_tool_is_never_forwarded_or_looked_up(db):
    state = make_state(db)
    result = await call_tool(make_ctx(state, make_agent(db, ["get_order"])), refund(value="900.00"))
    assert body(result)["code"] == "E_TOOL_NOT_ALLOWED"
    assert state.downstream.calls == [] and state.facts.lookups == 0


# ---------------------------------------------------------------- ALLOW / BLOCK / REVIEW


@pytest.mark.anyio
async def test_allowed_refund_forwards_the_resolved_amount_and_commits(db):
    state = make_state(db)
    result = await call_tool(make_ctx(state, make_agent(db)), refund())  # no amount: full remaining
    assert not result.is_error
    call_id = db.execute("SELECT id FROM tool_calls").fetchone()[0]
    assert state.downstream.calls == [(
        "create_refund",
        # The call ID rides along as PayPal's invoice_id, for reconciliation.
        {"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "40.00"}, "invoice_id": call_id},
    )]
    [entry] = ledger_rows(db)
    assert (entry["status"], entry["amount"], entry["refund_id"], entry["paypal_status"]) == (
        "COMMITTED", "40.00", "REF1", "COMPLETED"
    )
    call = db.execute("SELECT decision, outcome, resolved_amount FROM tool_calls").fetchone()
    assert tuple(call) == ("ALLOW", "OK", "40.00")


@pytest.mark.anyio
async def test_blocked_refund_returns_structured_error_and_sends_nothing(db):
    state = make_state(db)
    result = await call_tool(make_ctx(state, make_agent(db)), refund(value="900.00"))
    assert result.is_error
    error = body(result)
    assert error["decision"] == "BLOCK" and error["paypal_call"] == "NOT_SENT"
    assert error["code"] == "E_EXCEEDS_CAPTURE"
    assert {"E_EXCEEDS_CAPTURE", "E_ABOVE_HARD_LIMIT", "E_DAILY_LIMIT"} <= set(error["codes"])
    assert all(code.startswith("E_") for code in error["codes"])
    assert error["retryable"] is False and error["message"]
    assert {reason["rule"] for reason in error["reasons"]} >= {"within_capture", "hard_limit"}
    assert state.downstream.calls == [] and ledger_rows(db) == []
    call = db.execute("SELECT decision, outcome, forwarded, checks FROM tool_calls").fetchone()
    assert (call["decision"], call["outcome"], call["forwarded"]) == ("BLOCK", "NOT_SENT", 0)
    assert any(check["result"] == "FAIL" for check in json.loads(call["checks"]))


@pytest.mark.anyio
async def test_refund_for_another_customer_is_blocked(db):
    state = make_state(db, facts=FakeFacts([capture("CAP_CARLA", "cust_carla", "160.00", "0")]))
    result = await call_tool(make_ctx(state, make_agent(db)), refund("CAP_CARLA", "20.00"))
    assert body(result)["code"] == "E_SUBJECT_MISMATCH"
    assert state.downstream.calls == []


@pytest.mark.anyio
async def test_review_is_queued_with_the_exact_call_and_not_sent(db):
    state = make_state(db, facts=FakeFacts([capture("CAP_CARLA", "cust_carla", "160.00", "0")]))
    result = await call_tool(make_ctx(state, make_agent(db), "cust_carla"), refund("CAP_CARLA", "160.00"))
    assert not result.is_error
    pending = body(result)
    assert pending["decision"] == "REVIEW" and pending["status"] == "PENDING_OWNER_APPROVAL"
    assert state.downstream.calls == [] and ledger_rows(db) == []
    review = db.execute("SELECT * FROM reviews").fetchone()
    assert review["id"] == pending["review_id"] and review["status"] == "PENDING"
    assert json.loads(review["arguments"])["amount"]["value"] == "160.00"
    assert db.execute("SELECT outcome FROM tool_calls").fetchone()[0] == "AWAITING_REVIEW"


# ---------------------------------------------------------------- ledger


@pytest.mark.anyio
async def test_second_refund_for_same_customer_is_blocked_by_ledger(db):
    state = make_state(db, facts=FakeFacts([capture("CAP1"), capture("CAP2")]))
    agent = make_agent(db)
    assert not (await call_tool(make_ctx(state, agent), refund("CAP1", "10.00"))).is_error
    second = await call_tool(make_ctx(state, agent), refund("CAP2", "10.00"))
    assert body(second)["code"] == "E_FREQUENCY"


@pytest.mark.anyio
async def test_pending_review_counts_toward_frequency(db):
    facts = FakeFacts([capture("BIG", "cust_carla", "160.00", "0"), capture("SMALL", "cust_carla", "20.00", "0")])
    state = make_state(db, facts=facts)
    agent = make_agent(db)
    await call_tool(make_ctx(state, agent, "cust_carla"), refund("BIG", "160.00"))
    asked_again = await call_tool(make_ctx(state, agent, "cust_carla"), refund("SMALL", "20.00"))
    assert body(asked_again)["code"] == "E_FREQUENCY"


@pytest.mark.anyio
async def test_paypal_error_releases_the_reservation(db):
    state = make_state(db, downstream=FakeDownstream(paypal_error=True))
    agent = make_agent(db)
    result = await call_tool(make_ctx(state, agent), refund(value="10.00"))
    assert result.is_error
    assert ledger_rows(db)[0]["status"] == "RELEASED"
    state.downstream = FakeDownstream()
    assert not (await call_tool(make_ctx(state, agent), refund(value="10.00"))).is_error


@pytest.mark.anyio
async def test_paypal_error_reported_as_an_ordinary_result_still_releases(db):
    # PayPal's toolkit returns API errors as {"ok": false, ...} without flagging an MCP error.
    class ToolkitError(FakeDownstream):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            body = {"ok": False, "status": 422, "code": "PAYPAL_API_HTTP_ERROR", "message": "REFUND_DECLINED"}
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))])

    state = make_state(db, downstream=ToolkitError())
    await call_tool(make_ctx(state, make_agent(db)), refund(value="10.00"))
    assert ledger_rows(db)[0]["status"] == "RELEASED"
    assert db.execute("SELECT outcome FROM tool_calls").fetchone()[0] == "PAYPAL_ERROR"


@pytest.mark.anyio
async def test_unreachable_paypal_releases_and_reports_not_sent(db):
    state = make_state(db, downstream=FakeDownstream(error=DownstreamUnavailable("down")))
    result = await call_tool(make_ctx(state, make_agent(db)), refund(value="10.00"))
    assert body(result)["code"] == "E_DOWNSTREAM_UNAVAILABLE"
    assert ledger_rows(db)[0]["status"] == "RELEASED"
    assert db.execute("SELECT outcome FROM tool_calls").fetchone()[0] == "NOT_SENT"


@pytest.mark.anyio
async def test_unknown_outcome_still_counts_as_spent(db):
    state = make_state(db, downstream=FakeDownstream(error=TimeoutError()), facts=FakeFacts([capture("CAP1"), capture("CAP2")]))
    agent = make_agent(db)
    with pytest.raises(TimeoutError):
        await call_tool(make_ctx(state, agent), refund("CAP1", "10.00"))
    assert ledger_rows(db)[0]["status"] == "UNKNOWN"
    assert db.execute("SELECT outcome FROM tool_calls").fetchone()[0] == "OUTCOME_UNKNOWN"
    state.downstream = FakeDownstream()
    assert body(await call_tool(make_ctx(state, agent), refund("CAP2", "10.00")))["code"] == "E_FREQUENCY"


@pytest.mark.anyio
async def test_agent_disconnect_mid_refund_still_records_paypal_outcome(db):
    class SlowDownstream(FakeDownstream):
        async def call_tool(self, name, arguments):
            started.set()
            await anyio.sleep(0.1)
            return await super().call_tool(name, arguments)

    started = anyio.Event()
    state = make_state(db, downstream=SlowDownstream())
    agent = make_agent(db)
    async with anyio.create_task_group() as tg:
        tg.start_soon(call_tool, make_ctx(state, agent), refund(value="10.00"))
        await started.wait()
        tg.cancel_scope.cancel()  # the MCP request is cancelled while PayPal is working
    [entry] = ledger_rows(db)
    assert (entry["status"], entry["refund_id"]) == ("COMMITTED", "REF1")
    assert db.execute("SELECT outcome FROM tool_calls").fetchone()[0] == "OK"


# ---------------------------------------------------------------- races


@pytest.mark.anyio
async def test_concurrent_refunds_cannot_overspend_the_daily_cap(db):
    # 10 customers' agents refund $40 each at once against a $300/day cap: exactly 7 may pass.
    captures = [capture(f"CAP{i}", "cust_ben", "50.00", "0") for i in range(10)]
    state = make_state(db, facts=FakeFacts(captures, delay=0.05), mandate=NO_FREQUENCY)
    agent = make_agent(db)
    results = []

    async def one(i):
        results.append(await call_tool(make_ctx(state, agent), refund(f"CAP{i}", "40.00")))

    async with anyio.create_task_group() as task_group:
        for i in range(10):
            task_group.start_soon(one, i)

    allowed = [result for result in results if not result.is_error]
    blocked = [body(result)["code"] for result in results if result.is_error]
    assert len(allowed) == 7 and blocked == ["E_DAILY_LIMIT"] * 3
    assert len(state.downstream.calls) == 7
    assert sum(Decimal(row["amount"]) for row in ledger_rows(db)) == Decimal("280.00")


def test_ledger_reservations_are_atomic_across_connections(tmp_path):
    # Same race, but separate SQLite connections in separate threads (as with several workers).
    path = tmp_path / "race.db"
    store.connect(path)
    agent = AgentView("support-agent", True, frozenset({"create_refund"}))
    outcomes, barrier = [], threading.Barrier(10)

    def worker(i):
        db = store.connect(path)
        ledger = Ledger(db)
        facts = capture(f"CAP{i}", "cust_ben", "50.00", "0")
        barrier.wait()
        with ledger.transaction():
            decision = evaluate(
                EvaluationInput(
                    agent=agent,
                    mandate=NO_FREQUENCY,
                    tool="create_refund",
                    arguments={"capture_id": f"CAP{i}", "amount": {"currency_code": "USD", "value": "40.00"}},
                    session_customer="cust_ben",
                    facts=facts,
                    ledger=ledger.snapshot(NO_FREQUENCY, "cust_ben", NOW),
                    known_customer_emails=frozenset(),
                    now=NOW,
                )
            )
            if decision.outcome is Outcome.ALLOW:
                ledger.reserve(f"call{i}", NO_FREQUENCY, "support-agent", "cust_ben", f"CAP{i}", Decimal("40.00"), NOW)
        outcomes.append(decision.outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(Outcome.ALLOW) == 7
