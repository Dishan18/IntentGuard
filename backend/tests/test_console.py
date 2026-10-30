import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from starlette.applications import Starlette
from test_gateway import FakeFacts, body, capture, make_agent, make_ctx, make_state, refund
from test_owner import carla_capture, held_refund

from intentguard import store
from intentguard.gateway import console, owner, registry
from intentguard.gateway.app import AuthMiddleware
from intentguard.gateway.server import call_tool


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


async def scenario(db):
    """Ben gets $20 back, then tries $900; Carla's $160 is held and Maya approves it."""
    state = make_state(db, facts=FakeFacts([capture(), carla_capture()]))
    # Audit rows are stamped with the real time, so "today" must be the real today.
    state.clock = lambda: datetime.now(UTC)
    agent = make_agent(db)
    allowed = await call_tool(make_ctx(state, agent), refund(value="20.00"))
    blocked = await call_tool(make_ctx(state, agent), refund(value="900.00"))
    review_id = await held_refund(state, db)
    await owner.approve(state, review_id, "maya", "fair enough")
    return state, body(allowed), body(blocked), review_id


def by_id(rows):
    return {row["id"]: row for row in rows}


@pytest.mark.anyio
async def test_grid_rows_say_what_happened(db):
    state, allowed, blocked, review_id = await scenario(db)
    rows = console.decisions(db)
    assert len(rows) == 4
    assert rows[0]["owner_approved"] and rows[0]["decision"] == "ALLOW"  # newest first: the approved execution

    grid = by_id(rows)
    ben_ok = next(r for r in rows if r["amount"] == "20.00")
    assert (ben_ok["paypal_call"], ben_ok["paypal_id"], ben_ok["status"]) == ("SENT", allowed["id"], "COMPLETED")
    assert ben_ok["customer_name"] == "Ben Okafor" and not ben_ok["owner_approved"]

    stopped = grid[blocked["call_id"]]
    assert (stopped["decision"], stopped["rule"], stopped["amount"]) == ("BLOCK", "E_EXCEEDS_CAPTURE", "900.00")
    assert (stopped["paypal_call"], stopped["paypal_id"], stopped["status"]) == ("NOT_SENT", None, "STOPPED")

    held = next(r for r in rows if r["decision"] == "REVIEW")
    assert (held["review_id"], held["review_status"], held["status"]) == (review_id, "APPROVED", "APPROVED")


@pytest.mark.anyio
async def test_detail_answers_the_four_audit_questions(db):
    state, _, blocked, review_id = await scenario(db)

    stopped = console.decision_detail(db, blocked["call_id"], state.mandates)
    assert "Refund up to $50" in stopped["mandate"]["source_text"]  # 1. what the owner authorized
    assert stopped["mandate"]["refunds"]["daily_total_max"] == "300"
    assert stopped["arguments"]["amount"]["value"] == "900.00"  # 2. what the agent tried, verbatim
    assert stopped["paypal_response"] is None  # 3. nothing was sent, so PayPal said nothing
    failed = {c["rule"] for c in stopped["checks"] if c["result"] == "FAIL"}  # 4. rule by rule
    assert {"within_capture", "hard_limit"} <= failed

    execution = next(r for r in console.decisions(db) if r["owner_approved"])
    executed = console.decision_detail(db, execution["id"], state.mandates)
    assert executed["paypal_response"]["status"] == "COMPLETED"
    assert executed["review"]["review_id"] == review_id and executed["review"]["note"] == "fair enough"
    assert executed["ledger"]["status"] == "COMMITTED"
    assert any("approved by maya: fair enough" in item["what"] for item in executed["timeline"])
    assert console.decision_detail(db, "call_nope", state.mandates) is None


@pytest.mark.anyio
async def test_summary_counts_today_and_money_protected(db):
    state, *_ = await scenario(db)
    summary = console.snapshot(state)["summary"]
    assert summary["refunds_auto"] == {"count": 1, "amount": "20.00"}
    assert summary["refunds_owner_approved"] == {"count": 1, "amount": "160.00"}
    assert summary["held_for_review"]["count"] == 1 and summary["pending_reviews"] == 0
    assert summary["blocked"] == {"count": 1} and summary["protected"] == "900.00"
    [cap] = summary["daily_caps"]
    assert (Decimal(cap["used"]), Decimal(cap["cap"])) == (Decimal("180"), Decimal("300"))


@pytest.mark.anyio
async def test_console_data_needs_the_owner_key(db):
    state, *_ = await scenario(db)
    owner_key = registry.register_owner(db, "maya")
    agent_key = registry.register(db, "support-agent", "S", "o", "p", ["create_refund"])
    app = AuthMiddleware(Starlette(routes=console.routes(lambda: state)), db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as http:
        assert (await http.get("/owner/decisions")).status_code == 401
        assert (await http.get("/owner/decisions", headers={"Authorization": f"Bearer {agent_key}"})).status_code == 401
        headers = {"Authorization": f"Bearer {owner_key}"}
        assert len((await http.get("/owner/decisions?limit=2", headers=headers)).json()) == 2
        assert (await http.get("/owner/decisions/call_nope", headers=headers)).status_code == 404
        assert (await http.get("/owner/summary", headers=headers)).json()["blocked"]["count"] == 1


class FakeRequest:
    """Stays connected for a few checks, then goes away."""

    def __init__(self, ticks):
        self.ticks = ticks

    async def is_disconnected(self):
        self.ticks -= 1
        return self.ticks < 0


@pytest.mark.anyio
async def test_stream_sends_a_snapshot_then_only_changes(db, monkeypatch):
    monkeypatch.setattr(console, "STREAM_TICK_S", 0.01)
    state = make_state(db, facts=FakeFacts([capture()]))
    state.clock = lambda: datetime.now(UTC)
    stream_route = next(r for r in console.routes(lambda: state) if r.path == "/owner/stream")
    response = await stream_route.endpoint(FakeRequest(ticks=6))

    events = []
    async for chunk in response.body_iterator:
        events.append(chunk)
        if len(events) == 1:  # a decision lands while the console is watching
            await call_tool(make_ctx(state, make_agent(db)), refund(value="900.00"))

    snapshots = [json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: snapshot")]
    assert len(snapshots) == 2  # the first view, then one update; unchanged ticks send nothing
    assert snapshots[0]["decisions"] == [] and snapshots[1]["decisions"][0]["decision"] == "BLOCK"
    assert snapshots[1]["summary"]["protected"] == "900.00"
