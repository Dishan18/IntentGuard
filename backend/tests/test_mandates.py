import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from test_gateway import FakeDownstream, FakeFacts, body, capture, make_agent, make_ctx, refund

from intentguard import store
from intentguard.agents.llm import Generation
from intentguard.compiler.mandate import POLICY_SCHEMA, build, compile_mandate, render_rules, stated_numbers
from intentguard.demo import BILLING_MANDATE, MANDATES, SUPPORT_MANDATE
from intentguard.gateway import mandate_routes, registry
from intentguard.gateway.app import AuthMiddleware
from intentguard.gateway.mandates import MandateError, MandateStore
from intentguard.gateway.server import GatewayState, call_tool

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SUPPORT_TOOLS = ["create_refund", "get_order", "get_refund"]
BILLING_TOOLS = ["create_invoice", "send_invoice", "send_invoice_reminder", "get_invoice"]

SUPPORT_JSON = {
    "refunds": {"auto_approve_max": 50, "review_max": 200, "max_order_age_days": 30, "max_refunds_per_customer": 1,
                "per_customer_window_days": 30, "daily_total_max": 300},
    "invoices": None,
    "mapping": [{"text": "Refund up to $50 automatically", "becomes": "refunds.auto_approve_max"}],
    "unsupported": [],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


def agent(db, agent_id="support-agent", tools=SUPPORT_TOOLS):
    registry.register(db, agent_id, agent_id, "o", "p", tools)
    return registry.get(db, agent_id)


def with_refunds(**changes):
    data = json.loads(json.dumps(SUPPORT_JSON))
    data["refunds"].update(changes)
    return data


# ---------------------------------------------------------------- grounding: numbers must come from the owner's text


def test_stated_numbers_reads_digits_words_and_periods():
    found = stated_numbers("Refund up to $1,250.50, one refund per customer per month, twice a week")
    assert {Decimal("1250.50"), Decimal(1), Decimal(30), Decimal(2), Decimal(7)} <= found


def test_compiled_demo_text_is_grounded_and_matches_the_mandate():
    compiled = build(SUPPORT_JSON, SUPPORT_MANDATE.source_text, "support-agent", "mnd_support",
                     None, SUPPORT_MANDATE, "fake")
    assert compiled.blocking == [] and compiled.changes == []
    assert compiled.mandate.refunds == SUPPORT_MANDATE.refunds


def test_a_limit_the_model_invented_blocks_activation():
    compiled = build(with_refunds(daily_total_max=500), SUPPORT_MANDATE.source_text, "support-agent",
                     "mnd_support", None, SUPPORT_MANDATE, "fake")
    assert any("daily_total_max is 500" in problem for problem in compiled.blocking)


def test_no_approval_band_needs_no_number_of_its_own():
    text = "Refund up to $40 automatically, nothing more. At most $100 a day."
    data = {"refunds": {"auto_approve_max": 40, "review_max": 40, "max_order_age_days": None,
                        "max_refunds_per_customer": None, "per_customer_window_days": 30, "daily_total_max": 100},
            "invoices": None, "mapping": [], "unsupported": []}
    assert build(data, text, "support-agent", "mnd_support", None, None, "fake").blocking == []


def test_invalid_policy_is_never_a_draft():
    compiled = build(with_refunds(review_max=10), SUPPORT_MANDATE.source_text, "support-agent", "mnd_support",
                     None, None, "fake")
    assert compiled.mandate is None and "valid policy" in compiled.blocking[0]


def test_unsupported_clauses_and_capability_gaps_are_warned_about(db):
    data = dict(SUPPORT_JSON, unsupported=["Never accept a dispute over $100 without me."])
    billing = agent(db, "billing-agent", BILLING_TOOLS)
    compiled = build(data, SUPPORT_MANDATE.source_text + " Never accept a dispute over $100 without me.",
                     "billing-agent", "mnd_billing", billing, None, "fake")
    assert any("not enforced" in w and "dispute" in w for w in compiled.warnings)
    assert any("isn't allowed to call refund tools" in w for w in compiled.warnings)


def test_changes_against_the_active_mandate_are_listed():
    text = "Refunds up to $80 automatically, up to $200 with my approval, orders under 30 days, " \
           "one refund per customer per 30 days, $300 a day."
    compiled = build(with_refunds(auto_approve_max=80), text, "support-agent", "mnd_support", None, SUPPORT_MANDATE, "fake")
    assert compiled.changes == [{"field": "refunds.auto_approve_max", "from": "50", "to": "80"}]


# ---------------------------------------------------------------- shown back to the owner


def test_rules_read_back_in_plain_english():
    rules = render_rules(BILLING_MANDATE)
    assert rules[0] == "No refunds: every refund attempt is blocked."
    assert "Invoices above $1,000.00 and up to $2,000.00 wait for your approval." in rules
    assert "Invoices only to existing customers." in rules


def test_example_decisions_come_from_the_real_engine(db):
    compiled = build(SUPPORT_JSON, SUPPORT_MANDATE.source_text, "support-agent", "mnd_support",
                     agent(db), SUPPORT_MANDATE, "fake")
    outcomes = {example["scenario"]: (example["decision"], example["codes"]) for example in compiled.examples}
    assert outcomes["A $20.00 refund on a recent order"] == ("ALLOW", [])
    assert outcomes["A $125.00 refund"][0] == "REVIEW"
    assert outcomes["A $300.00 refund"][0] == "BLOCK"
    assert "E_SUBJECT_MISMATCH" in outcomes["A refund on another customer's order"][1]
    assert "E_DAILY_LIMIT" in outcomes["A $20.00 refund after $300.00 was already refunded today"][1]


# ---------------------------------------------------------------- the LLM call


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def generate(self, system, contents, tools, pin_model=None, response_schema=None):
        self.calls.append({"system": system, "prompt": contents[0]["parts"][0]["text"], "schema": response_schema})
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return Generation({"role": "model", "parts": [{"text": text}]}, "fake-model")


@pytest.mark.anyio
async def test_compiler_asks_for_schema_constrained_json(db):
    llm = FakeLLM(SUPPORT_JSON)
    compiled = await compile_mandate(llm, SUPPORT_MANDATE.source_text, "support-agent", "mnd_support", agent(db), None)
    [call] = llm.calls
    assert call["schema"] is POLICY_SCHEMA
    assert "create_refund" in call["prompt"] and SUPPORT_MANDATE.source_text in call["prompt"]
    assert compiled.model == "fake-model" and compiled.mandate.refunds.daily_total_max == Decimal("300")


@pytest.mark.anyio
async def test_non_json_reply_is_reported_not_stored(db):
    compiled = await compile_mandate(FakeLLM("sure! here you go"), "x", "support-agent", "mnd_support", None, None)
    assert compiled.mandate is None and compiled.blocking


# ---------------------------------------------------------------- versions and activation


def test_seed_then_draft_then_activate_supersedes(db):
    mandates = MandateStore(db)
    mandates.seed(MANDATES.values(), NOW)
    mandates.seed(MANDATES.values(), NOW)  # idempotent
    assert mandates.active("support-agent").version == 1

    raised = SUPPORT_MANDATE.model_copy(update={"refunds": SUPPORT_MANDATE.refunds.model_copy(update={"auto_approve_max": Decimal("80")})})
    version = mandates.create_draft(raised, "fake", {}, NOW)
    assert version == 2 and mandates.active("support-agent").refunds.auto_approve_max == Decimal("50")  # drafts aren't enforced

    mandates.activate("mnd_support", 2, "maya", NOW)
    assert mandates.active("support-agent").refunds.auto_approve_max == Decimal("80")
    statuses = {row["version"]: row["status"] for row in mandates.history("support-agent")}
    assert statuses == {1: "SUPERSEDED", 2: "ACTIVE"}
    with pytest.raises(MandateError):
        mandates.activate("mnd_support", 1, "maya", NOW)  # only drafts can be activated


def test_a_draft_with_blocking_problems_cannot_be_activated(db):
    mandates = MandateStore(db)
    version = mandates.create_draft(SUPPORT_MANDATE, "fake", {"blocking": ["invented number"]}, NOW)
    with pytest.raises(MandateError, match="invented number"):
        mandates.activate("mnd_support", version, "maya", NOW)
    assert mandates.active("support-agent") is None


@pytest.mark.anyio
async def test_gateway_enforces_the_active_version_and_records_it(db):
    mandates = MandateStore(db)
    mandates.seed(MANDATES.values(), NOW)
    state = GatewayState(db=db, downstream=FakeDownstream(), facts=FakeFacts([capture()]), mandates=mandates.active,
                         clock=lambda: NOW)
    support = make_agent(db)

    first = await call_tool(make_ctx(state, support), refund(value="30.00"))
    assert not first.is_error
    lowered = SUPPORT_MANDATE.model_copy(update={
        "refunds": SUPPORT_MANDATE.refunds.model_copy(update={"auto_approve_max": Decimal("5"), "max_refunds_per_customer": None})
    })
    mandates.activate("mnd_support", mandates.create_draft(lowered, "fake", {}, NOW), "maya", NOW)
    second = body(await call_tool(make_ctx(state, support), refund(value="10.00")))
    assert second["decision"] == "REVIEW"  # $10 is now above the new $5 auto limit

    pinned = [tuple(r) for r in db.execute("SELECT mandate_id, mandate_version FROM tool_calls ORDER BY rowid")]
    assert pinned[-2:] == [("mnd_support", 1), ("mnd_support", 2)]


# ---------------------------------------------------------------- owner API


def api(db, llm):
    state = SimpleNamespace(db=db, clock=lambda: NOW, compiler_llm=llm)
    MandateStore(db).seed(MANDATES.values(), NOW)
    app = AuthMiddleware(Starlette(routes=mandate_routes.routes(lambda: state)), db)
    key = registry.register_owner(db, "maya")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test",
                             headers={"Authorization": f"Bearer {key}"})


@pytest.mark.anyio
async def test_compile_creates_a_draft_and_only_activate_switches(db):
    agent(db)
    text = "Refunds up to $80 automatically, up to $200 with my approval, orders under 30 days, " \
           "one refund per customer per 30 days, at most $300 a day."
    async with api(db, FakeLLM(with_refunds(auto_approve_max=80))) as http:
        draft = (await http.post("/owner/mandates/compile", json={"agent_id": "support-agent", "text": text})).json()
        assert (draft["status"], draft["version"], draft["blocking"]) == ("DRAFT", 2, [])
        assert draft["changes"] == [{"field": "refunds.auto_approve_max", "from": "50", "to": "80"}]
        assert draft["rules"][0] == "Refunds up to $80.00 go through automatically."
        assert MandateStore(db).active("support-agent").version == 1

        activated = await http.post("/owner/mandates/mnd_support/2/activate")
        assert activated.status_code == 200 and activated.json()["activated_by"] == "maya"
        overview = (await http.get("/owner/mandates")).json()
    support = next(a for a in overview if a["agent"]["id"] == "support-agent")
    assert support["active"]["version"] == 2 and [h["status"] for h in support["history"]] == ["ACTIVE", "SUPERSEDED"]


@pytest.mark.anyio
async def test_ungrounded_draft_is_stored_but_refused_at_activation(db):
    agent(db)
    async with api(db, FakeLLM(with_refunds(daily_total_max=999))) as http:
        draft = (await http.post("/owner/mandates/compile",
                                 json={"agent_id": "support-agent", "text": SUPPORT_MANDATE.source_text})).json()
        assert draft["blocking"]
        refused = await http.post(f"/owner/mandates/mnd_support/{draft['version']}/activate")
    assert refused.status_code == 409
    assert MandateStore(db).active("support-agent").version == 1


@pytest.mark.anyio
async def test_compile_needs_a_known_agent_text_and_an_llm(db):
    agent(db)
    async with api(db, None) as http:
        assert (await http.post("/owner/mandates/compile", json={"agent_id": "nobody", "text": "x"})).status_code == 404
        assert (await http.post("/owner/mandates/compile", json={"agent_id": "support-agent", "text": ""})).status_code == 400
        assert (await http.post("/owner/mandates/compile", json={"agent_id": "support-agent", "text": "x"})).status_code == 503


@pytest.mark.anyio
async def test_mandate_api_needs_the_owner_key(db):
    agent_key = registry.register(db, "support-agent", "S", "o", "p", SUPPORT_TOOLS)
    async with api(db, FakeLLM(SUPPORT_JSON)) as http:
        response = await http.post("/owner/mandates/mnd_support/1/activate", headers={"Authorization": f"Bearer {agent_key}"})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_restore_brings_a_past_version_back_as_a_draft_without_the_llm(db):
    agent(db)
    text = "Refunds up to $80 automatically, up to $200 with my approval, orders under 30 days, " \
           "one refund per customer per 30 days, at most $300 a day."
    llm = FakeLLM(with_refunds(auto_approve_max=80))
    async with api(db, llm) as http:
        draft = (await http.post("/owner/mandates/compile", json={"agent_id": "support-agent", "text": text})).json()
        await http.post(f"/owner/mandates/mnd_support/{draft['version']}/activate")
        restored = (await http.post("/owner/mandates/mnd_support/1/restore")).json()
        assert (restored["status"], restored["version"], restored["compiled_by"]) == ("DRAFT", 3, "restored from v1")
        assert restored["changes"] == [{"field": "refunds.auto_approve_max", "from": "80", "to": "50"}]
        assert MandateStore(db).active("support-agent").version == 2  # still only a draft
        await http.post("/owner/mandates/mnd_support/3/activate")
    assert len(llm.calls) == 1  # restoring didn't call the model
    assert MandateStore(db).active("support-agent").refunds == SUPPORT_MANDATE.refunds
