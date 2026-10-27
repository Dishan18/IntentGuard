import json
from types import SimpleNamespace

import mcp_types as types
import pytest

from intentguard import store
from intentguard.gateway import registry
from intentguard.gateway.downstream import DownstreamUnavailable
from intentguard.gateway.server import AGENT_SCOPE_KEY, GatewayState, call_tool, list_tools


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


class FakeDownstream:
    def __init__(self, behaviour=None):
        self.tools = {name: types.Tool(name=name, input_schema={"type": "object"}) for name in ("get_order", "create_refund")}
        self.calls = []
        self._behaviour = behaviour

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._behaviour:
            raise self._behaviour
        return types.CallToolResult(content=[types.TextContent(type="text", text='{"status": "COMPLETED"}')])


def make_ctx(db, downstream, agent, customer="cust_ben"):
    request = SimpleNamespace(scope={AGENT_SCOPE_KEY: agent}, headers={"x-intentguard-customer": customer})
    return SimpleNamespace(lifespan_context=GatewayState(db=db, downstream=downstream), request=request)


def make_agent(db, tools):
    key = registry.register(db, "agent-1", "Agent", "owner", "purpose", tools)
    return registry.authenticate(db, key)


def refund_params(value="5.00"):
    return types.CallToolRequestParams(
        name="create_refund", arguments={"capture_id": "CAP123", "amount": {"currency_code": "USD", "value": value}}
    )


def only_row(db):
    rows = db.execute("SELECT * FROM tool_calls").fetchall()
    assert len(rows) == 1
    return rows[0]


def test_key_is_hashed_and_rekey_revokes_old_key(db):
    first = registry.register(db, "a", "A", "o", "p", ["get_order"])
    stored = db.execute("SELECT key_hash FROM agents").fetchone()["key_hash"]
    assert first not in stored
    second = registry.register(db, "a", "A", "o", "p", ["get_order"])
    assert registry.authenticate(db, first) is None
    assert registry.authenticate(db, second).allowed_tools == frozenset({"get_order"})


@pytest.mark.anyio
async def test_list_tools_filters_to_allowlist(db):
    agent = make_agent(db, ["get_order"])
    result = await list_tools(make_ctx(db, FakeDownstream(), agent), None)
    assert [tool.name for tool in result.tools] == ["get_order"]


@pytest.mark.anyio
async def test_disallowed_tool_is_never_forwarded(db):
    downstream = FakeDownstream()
    agent = make_agent(db, ["get_order"])
    result = await call_tool(make_ctx(db, downstream, agent), refund_params("900.00"))
    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "E_TOOL_NOT_ALLOWED"
    assert downstream.calls == []
    row = only_row(db)
    assert (row["decision"], row["forwarded"], row["outcome"]) == ("BLOCK", 0, "NOT_SENT")


@pytest.mark.anyio
async def test_allowed_call_is_forwarded_and_audited(db):
    downstream = FakeDownstream()
    agent = make_agent(db, ["create_refund"])
    result = await call_tool(make_ctx(db, downstream, agent), refund_params())
    assert not result.is_error
    assert downstream.calls == [("create_refund", refund_params().arguments)]
    row = only_row(db)
    assert (row["customer_id"], row["forwarded"], row["outcome"]) == ("cust_ben", 1, "OK")


@pytest.mark.anyio
async def test_unreachable_paypal_is_recorded_as_not_sent(db):
    agent = make_agent(db, ["create_refund"])
    result = await call_tool(make_ctx(db, FakeDownstream(DownstreamUnavailable("down")), agent), refund_params())
    assert json.loads(result.content[0].text)["code"] == "E_DOWNSTREAM_UNAVAILABLE"
    assert only_row(db)["outcome"] == "NOT_SENT"


@pytest.mark.anyio
async def test_failure_after_forwarding_is_outcome_unknown(db):
    agent = make_agent(db, ["create_refund"])
    with pytest.raises(TimeoutError):
        await call_tool(make_ctx(db, FakeDownstream(TimeoutError()), agent), refund_params())
    row = only_row(db)
    assert (row["forwarded"], row["outcome"]) == (1, "OUTCOME_UNKNOWN")
