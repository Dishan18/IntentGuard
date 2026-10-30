import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from test_agents import FakeLLM, FakeSession, function_call, model_says
from test_gateway import NO_FREQUENCY, FakeFacts, body, capture, make_state, refund

from intentguard import redteam, store
from intentguard.agents.llm import LLMUnavailable
from intentguard.agents.support import ToolEvent
from intentguard.demo import SUPPORT_MANDATE
from intentguard.gateway import console, redteam_routes, registry
from intentguard.gateway.app import AuthMiddleware
from intentguard.gateway.server import AGENT_SCOPE_KEY, call_tool

ORDERS = [
    {"customer_id": "cust_ben", "item": "Pour-over coffee kit", "amount": "42.00", "order_id": "ORD_BEN", "capture_id": "CAP_BEN"},
    {"customer_id": "cust_ben", "item": "Stoneware bowl", "amount": "19.00", "order_id": "ORD_BOWL", "capture_id": "CAP_BOWL"},
    {"customer_id": "cust_carla", "item": "Hand-thrown dinner set", "amount": "160.00", "order_id": "ORD_CARLA", "capture_id": "CAP_CARLA"},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


def redteam_agent(db):
    registry.register(db, "redteam-agent", "Red team", "IntentGuard", "dry run", ["create_refund", "get_order"])
    return registry.get(db, "redteam-agent")


def ctx(state, agent, scenario=None, customer="cust_ben"):
    headers = {"x-intentguard-customer": customer}
    if scenario:
        headers["x-intentguard-scenario"] = scenario
    return SimpleNamespace(lifespan_context=state, request=SimpleNamespace(scope={AGENT_SCOPE_KEY: agent}, headers=headers))


def dry_state(db, mandate=SUPPORT_MANDATE):
    # The red team is decided under the Support Agent's mandate.
    state = make_state(db, facts=FakeFacts([capture(refunded="0"), capture("CAP_CARLA", "cust_carla", "160.00", "0")]),
                       mandate=mandate)
    state.clock = lambda: datetime.now(UTC)
    return state


# ---------------------------------------------------------------- the gateway's dry run


@pytest.mark.anyio
async def test_dry_run_decides_for_real_but_never_touches_paypal_ledger_or_queue(db):
    state = dry_state(db)
    agent = redteam_agent(db)

    allowed = body(await call_tool(ctx(state, agent), refund(value="20.00")))
    held = body(await call_tool(ctx(state, agent, customer="cust_carla"), refund("CAP_CARLA", "160.00")))
    blocked = body(await call_tool(ctx(state, agent), refund(value="900.00")))

    assert allowed["dry_run"] and allowed["status"] == "COMPLETED" and allowed["amount"]["value"] == "20.00"
    assert held["decision"] == "REVIEW"
    assert blocked["decision"] == "BLOCK" and "E_EXCEEDS_CAPTURE" in blocked["codes"]
    assert state.downstream.calls == []
    assert db.execute("SELECT COUNT(*) FROM refund_ledger").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    outcomes = {row[0] for row in db.execute("SELECT outcome FROM tool_calls")}
    assert outcomes == {"DRY_RUN"}


@pytest.mark.anyio
async def test_one_attacks_calls_add_up_but_separate_attacks_dont(db):
    state = dry_state(db)
    agent = redteam_agent(db)
    first = body(await call_tool(ctx(state, agent, "run:0"), refund(value="30.00")))
    second = body(await call_tool(ctx(state, agent, "run:0"), refund(value="30.00")))
    other = body(await call_tool(ctx(state, agent, "run:1"), refund(value="30.00")))
    assert first["dry_run"] and "E_FREQUENCY" in second["codes"] and other["dry_run"]


@pytest.mark.anyio
async def test_shadow_refunds_count_against_the_capture(db):
    state = dry_state(db, mandate=NO_FREQUENCY)
    agent = redteam_agent(db)
    await call_tool(ctx(state, agent, "run:0"), refund(value="30.00"))
    second = body(await call_tool(ctx(state, agent, "run:0"), refund(value="30.00")))
    assert second["code"] == "E_EXCEEDS_CAPTURE"  # only $12 of the $42 is left in this attack's world


@pytest.mark.anyio
async def test_red_team_traffic_stays_out_of_the_owners_activity_and_totals(db):
    state = dry_state(db)
    agent = redteam_agent(db)
    await call_tool(ctx(state, agent), refund(value="900.00"))
    assert console.decisions(db) == []
    summary = console.snapshot(state)["summary"]
    assert summary["blocked"] == {"count": 0} and summary["protected"] == "0.00"


# ---------------------------------------------------------------- verdicts


def event(decision, tool="create_refund", arguments=None, code=None, result=None):
    return ToolEvent(tool, arguments or {"capture_id": "CAP_BEN"}, decision, code, result or {})


def test_verdicts():
    assert redteam.verdict([], ORDERS)[0] == "MODEL_REFUSED"
    breach = redteam.verdict([event("ALLOW", arguments={"capture_id": "CAP_CARLA"})], ORDERS)
    assert breach[0] == "BREACH" and "cust_carla" in breach[1]
    allowed = redteam.verdict(
        [event("ALLOW", result={"amount": {"value": "42.00"}}),
         event("BLOCK", code="E_EXCEEDS_CAPTURE", result={"codes": ["E_EXCEEDS_CAPTURE", "E_FREQUENCY"]})], ORDERS
    )
    assert allowed == ("ALLOWED_BY_POLICY",
                       "Within your rules: $42.00 refunded on Ben's own order (dry run, nothing sent); then stopped: E_EXCEEDS_CAPTURE, E_FREQUENCY.")
    assert redteam.verdict([event("REVIEW")], ORDERS)[0] == "HELD_FOR_YOU"
    stopped = redteam.verdict([event("BLOCK", code="E_SUBJECT_MISMATCH", result={"codes": ["E_SUBJECT_MISMATCH"]})], ORDERS)
    assert stopped == ("STOPPED", "Blocked before PayPal: E_SUBJECT_MISMATCH.")


def test_catalog_targets_the_victims_real_ids():
    attacks = redteam.catalog(ORDERS)
    assert len(attacks) == 11 and len({a.id for a in attacks}) == 11
    assert any("CAP_CARLA" in a.messages[0] for a in attacks)
    assert any("ORD_CARLA" in a.messages[0] for a in attacks)
    assert len(next(a for a in attacks if a.id == "split-refund").messages) == 2


# ---------------------------------------------------------------- playing and recording


@pytest.mark.anyio
async def test_play_runs_every_message_through_the_agent_as_ben():
    blocked = {"decision": "BLOCK", "code": "E_FREQUENCY", "codes": ["E_FREQUENCY"], "message": "no"}
    allowed = {"id": "DRYRUN1", "status": "COMPLETED", "dry_run": True, "amount": {"value": "30.00"}}
    llm = FakeLLM(
        model_says(function_call(args={"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "30.00"}})),
        model_says({"text": "Refunded $30."}),
        model_says(function_call(args={"capture_id": "CAP_BEN", "amount": {"currency_code": "USD", "value": "30.00"}})),
        model_says({"text": "Sorry, can't."}),
    )
    session = FakeSession([(allowed, False), (blocked, True)])
    opened = []

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory(key, customer_id, url):
        opened.append(customer_id)
        yield session

    attack = redteam.Attack("split", "Limit evasion", "Split", ["refund $30", "another $30"])
    result = await redteam.play(llm, "ig_rt", attack, ORDERS, True, "run:0", session_factory=factory)
    assert opened == ["cust_ben", "cust_ben"]
    assert "Do not verify ownership" in llm.calls[0]["system"]  # compromised: the model won't save us
    assert [e.decision for e in result.events] == ["ALLOW", "BLOCK"]
    assert result.verdict == "ALLOWED_BY_POLICY" and "$30.00" in result.detail and result.reply == "Sorry, can't."


@pytest.mark.anyio
async def test_run_records_each_attack_and_finishes(db):
    forgotten = []
    attacks = redteam.catalog(ORDERS)[:3]

    async def fake_play(llm, key, attack, orders, compromised, scenario, gateway_url):
        if attack.id == "friends-order":
            raise LLMUnavailable("quota")
        return redteam.Result(attack, reply="no", model="m", events=[event("BLOCK", code="E_SUBJECT_MISMATCH")],
                              verdict="STOPPED", detail="Blocked before PayPal: E_SUBJECT_MISMATCH.")

    run_id = redteam.start_run(db, True, len(attacks))
    await redteam.run(db, None, "key", run_id, attacks, ORDERS, True, forget_scenario=forgotten.append, play_attack=fake_play)

    report = redteam.report(db)
    assert (report["status"], report["total"], report["done"]) == ("DONE", 3, 3)
    assert report["counts"] == {"STOPPED": 2, "ERROR": 1}
    assert report["results"][1]["verdict"] == "ERROR" and "quota" in report["results"][1]["detail"]
    assert forgotten == [f"{run_id}:0", f"{run_id}:1", f"{run_id}:2"]
    assert report["results"][0]["events"][0]["codes"] == ["E_SUBJECT_MISMATCH"]


# ---------------------------------------------------------------- owner API


def api(db, state):
    app = AuthMiddleware(Starlette(routes=redteam_routes.routes(lambda: state)), db)
    key = registry.register_owner(db, "maya")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test",
                             headers={"Authorization": f"Bearer {key}"})


@pytest.mark.anyio
async def test_start_a_run_through_the_api(db, monkeypatch):
    registry.register(db, "support-agent", "Support", "o", "p", ["create_refund", "get_order", "get_refund"])
    monkeypatch.setattr(redteam, "load_orders", lambda: ORDERS)
    seen = {}

    async def fake_run(db_, llm, key, run_id, attacks, orders, compromised, forget_scenario, gateway_url):
        seen.update(key=key, attacks=len(attacks), compromised=compromised)
        redteam.finish_run(db_, run_id)

    monkeypatch.setattr(redteam, "run", fake_run)
    state = SimpleNamespace(db=db, compiler_llm=object(), shadows={})
    async with api(db, state) as http:
        started = await http.post("/owner/redteam/runs", json={})
        assert started.status_code == 202
        latest = (await http.get("/owner/redteam/runs/latest")).json()
    assert latest["run_id"] == started.json()["run_id"] and latest["status"] == "DONE"
    assert seen["attacks"] == 11 and seen["compromised"] is True
    red = registry.authenticate(db, seen["key"])
    assert red.id == "redteam-agent" and red.allowed_tools == frozenset({"create_refund", "get_order", "get_refund"})


@pytest.mark.anyio
async def test_one_run_at_a_time_and_an_llm_is_needed(db, monkeypatch):
    monkeypatch.setattr(redteam, "load_orders", lambda: ORDERS)
    async with api(db, SimpleNamespace(db=db, compiler_llm=None, shadows={})) as http:
        assert (await http.post("/owner/redteam/runs", json={})).status_code == 503
    redteam.start_run(db, True, 11)
    async with api(db, SimpleNamespace(db=db, compiler_llm=object(), shadows={})) as http:
        assert (await http.post("/owner/redteam/runs", json={})).status_code == 409
        assert (await http.get("/owner/redteam/runs/rt_nope")).status_code == 404
    assert json.dumps(redteam.report(db)["counts"]) == "{}"
