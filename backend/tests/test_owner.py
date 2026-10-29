from datetime import timedelta

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from test_gateway import NOW, FakeDownstream, FakeFacts, body, capture, make_agent, make_ctx, make_state, refund

from intentguard.gateway import owner, registry
from intentguard.gateway.app import AuthMiddleware
from intentguard.gateway.server import call_tool

CARLA = "cust_carla"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    from intentguard import store

    return store.connect(tmp_path / "test.db")


def carla_capture(refunded="0"):
    return capture("CAP_CARLA", CARLA, "160.00", refunded)


async def held_refund(state, db, value="160.00"):
    """Carla asks for $160: above the $50 auto limit, so it is held for the owner."""
    result = await call_tool(make_ctx(state, make_agent(db), CARLA), refund("CAP_CARLA", value))
    held = body(result)
    assert held["decision"] == "REVIEW"
    return held["review_id"]


def row(db, sql, *params):
    return db.execute(sql, params).fetchone()


# ---------------------------------------------------------------- approve


@pytest.mark.anyio
async def test_approval_executes_the_stored_request_and_settles_everything(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    assert state.downstream.calls == []

    status, result = await owner.approve(state, review_id, "maya", "Wrong colour, fair enough")

    assert status == 200 and result["status"] == "APPROVED" and result["paypal"]["status"] == "COMPLETED"
    assert state.downstream.calls == [
        ("create_refund", {"capture_id": "CAP_CARLA", "amount": {"currency_code": "USD", "value": "160.00"}})
    ]
    review = row(db, "SELECT * FROM reviews WHERE id = ?", review_id)
    assert (review["status"], review["resolved_by"], review["note"]) == ("APPROVED", "maya", "Wrong colour, fair enough")
    executed = row(db, "SELECT * FROM tool_calls WHERE id = ?", review["execution_call_id"])
    assert (executed["decision"], executed["outcome"], executed["review_id"]) == ("ALLOW", "OK", review_id)
    assert row(db, "SELECT outcome FROM tool_calls WHERE id = ?", review["call_id"])[0] == "REVIEW_APPROVED"
    ledger = row(db, "SELECT * FROM refund_ledger")
    assert (ledger["status"], ledger["amount"], ledger["call_id"]) == ("COMMITTED", "160.00", review["execution_call_id"])


@pytest.mark.anyio
async def test_the_review_being_approved_does_not_count_against_its_own_frequency_limit(db):
    # The demo mandate allows one refund per customer; the pending review is that one.
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    _, result = await owner.approve(state, review_id, "maya")
    assert result["status"] == "APPROVED"


@pytest.mark.anyio
async def test_a_review_can_only_be_approved_once(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    await owner.approve(state, review_id, "maya")
    status, again = await owner.approve(state, review_id, "maya")
    assert status == 409 and again["status"] == "APPROVED"
    assert len(state.downstream.calls) == 1


@pytest.mark.anyio
async def test_concurrent_approvals_execute_once(db):
    state = make_state(db, facts=FakeFacts([carla_capture()], delay=0.05))
    review_id = await held_refund(state, db)
    results = []

    async def click():
        results.append(await owner.approve(state, review_id, "maya"))

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(click)
    assert sorted(status for status, _ in results) == [200, 409, 409, 409, 409]
    assert len(state.downstream.calls) == 1


# ---------------------------------------------------------------- re-check at approval time


@pytest.mark.anyio
async def test_approval_is_rechecked_against_fresh_paypal_facts(db):
    facts = FakeFacts([carla_capture()])
    state = make_state(db, facts=facts)
    review_id = await held_refund(state, db)
    facts.captures["CAP_CARLA"] = carla_capture(refunded="160.00")  # refunded elsewhere meanwhile

    _, result = await owner.approve(state, review_id, "maya")

    assert result["status"] == "FAILED" and "E_EXCEEDS_CAPTURE" in result["codes"]
    assert result["paypal_call"] == "NOT_SENT" and state.downstream.calls == []
    assert row(db, "SELECT status FROM reviews WHERE id = ?", review_id)[0] == "FAILED"
    assert row(db, "SELECT COUNT(*) FROM refund_ledger")[0] == 0


@pytest.mark.anyio
async def test_approval_respects_the_daily_cap_as_it_is_now(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    db.execute(
        "INSERT INTO refund_ledger VALUES ('other','mnd_support','support-agent','cust_dev','CAP_X','200.00',"
        "'COMMITTED','R9',?,?,?)",
        (NOW.date().isoformat(), NOW.isoformat(), NOW.isoformat()),
    )
    _, result = await owner.approve(state, review_id, "maya")
    assert result["status"] == "FAILED" and "E_DAILY_LIMIT" in result["codes"]
    assert state.downstream.calls == []


@pytest.mark.anyio
async def test_approval_covers_only_the_reasons_the_owner_saw(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    db.execute("UPDATE reviews SET codes = '[]' WHERE id = ?", (review_id,))
    _, result = await owner.approve(state, review_id, "maya")
    assert result["status"] == "FAILED" and result["codes"][0] == "E_REVIEW_CHANGED"
    assert state.downstream.calls == []


@pytest.mark.anyio
async def test_revoked_agent_cannot_have_its_request_approved(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    db.execute("UPDATE agents SET status = 'REVOKED'")
    _, result = await owner.approve(state, review_id, "maya")
    assert result["status"] == "FAILED" and state.downstream.calls == []


@pytest.mark.anyio
async def test_paypal_error_on_execution_fails_the_review_and_releases(db):
    state = make_state(db, downstream=FakeDownstream(paypal_error=True), facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    _, result = await owner.approve(state, review_id, "maya")
    assert result["status"] == "FAILED" and result["paypal_call"] == "SENT"
    assert row(db, "SELECT status FROM refund_ledger")[0] == "RELEASED"
    assert row(db, "SELECT status FROM reviews WHERE id = ?", review_id)[0] == "FAILED"


# ---------------------------------------------------------------- reject and expiry


@pytest.mark.anyio
async def test_reject_sends_nothing_and_frees_the_customer_limit(db):
    state = make_state(db, facts=FakeFacts([carla_capture(), capture("CAP_SMALL", CARLA, "20.00", "0")]))
    review_id = await held_refund(state, db)

    status, result = owner.reject(state, review_id, "maya", "Please return the set first")

    assert status == 200 and result["status"] == "REJECTED"
    assert state.downstream.calls == []
    review = row(db, "SELECT * FROM reviews WHERE id = ?", review_id)
    assert (review["status"], review["note"]) == ("REJECTED", "Please return the set first")
    assert row(db, "SELECT outcome FROM tool_calls WHERE id = ?", review["call_id"])[0] == "REVIEW_REJECTED"
    assert (await owner.approve(state, review_id, "maya"))[0] == 409
    # No longer pending, so it doesn't use up Carla's one refund.
    small = await call_tool(make_ctx(state, make_agent(db), CARLA), refund("CAP_SMALL", "20.00"))
    assert not small.is_error


@pytest.mark.anyio
async def test_stale_review_expires_and_cannot_be_approved(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    state.clock = lambda: NOW + timedelta(hours=73)

    status, result = await owner.approve(state, review_id, "maya")

    assert status == 409 and result["status"] == "EXPIRED"
    assert state.downstream.calls == []


@pytest.mark.anyio
async def test_list_shows_reasons_and_expires_stale_reviews(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    [pending] = owner.list_reviews(state, "PENDING")
    assert pending["review_id"] == review_id and pending["amount"] == "160.00"
    assert pending["reasons"] and pending["reasons"][0]["code"] in pending["codes"]
    state.clock = lambda: NOW + timedelta(hours=73)
    assert owner.list_reviews(state, "PENDING") == []
    assert owner.list_reviews(state)[0]["status"] == "EXPIRED"


# ---------------------------------------------------------------- HTTP and keys


def owner_client(db, state):
    app = AuthMiddleware(Starlette(routes=owner.routes(lambda: state)), db)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test")


@pytest.mark.anyio
async def test_owner_api_needs_an_owner_key_and_agent_keys_dont_work(db):
    state = make_state(db, facts=FakeFacts([carla_capture()]))
    review_id = await held_refund(state, db)
    agent_key = registry.register(db, "support-agent", "Support", "o", "p", ["create_refund"])
    owner_key = registry.register_owner(db, "maya")

    async with owner_client(db, state) as http:
        assert (await http.get("/owner/reviews")).status_code == 401
        approve_url = f"/owner/reviews/{review_id}/approve"
        as_agent = await http.post(approve_url, headers={"Authorization": f"Bearer {agent_key}"})
        assert as_agent.status_code == 401
        assert state.downstream.calls == []

        headers = {"Authorization": f"Bearer {owner_key}"}
        listed = (await http.get("/owner/reviews?status=pending", headers=headers)).json()
        assert [r["review_id"] for r in listed] == [review_id]
        approved = await http.post(approve_url, headers=headers, json={"note": "  ok  "})
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    assert row(db, "SELECT resolved_by, note FROM reviews")[:] == ("maya", "ok")


@pytest.mark.anyio
async def test_owner_key_is_not_an_agent_key(db):
    owner_key = registry.register_owner(db, "maya")
    app = AuthMiddleware(Starlette(), db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as http:
        response = await http.post("/mcp", headers={"Authorization": f"Bearer {owner_key}"}, json={})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_owner_api_is_503_until_the_gateway_is_running(db):
    owner_key = registry.register_owner(db, "maya")
    async with owner_client(db, None) as http:
        response = await http.get("/owner/reviews", headers={"Authorization": f"Bearer {owner_key}"})
    assert response.status_code == 503


def test_rekeying_the_owner_revokes_the_old_key(db):
    first = registry.register_owner(db, "maya")
    second = registry.register_owner(db, "maya")
    assert registry.authenticate_owner(db, first) is None
    assert registry.authenticate_owner(db, second) == "maya"
