import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import mcp_types as types
import pytest

from intentguard.agents.llm import GeminiClient, Generation, LLMUnavailable, tool_declaration
from intentguard.agents.support import Customer, SupportAgent

BEN = Customer("cust_ben", "Ben Okafor")
ORDERS = [{"item": "Pour-over coffee kit", "amount": "42.00", "order_id": "ORD1", "capture_id": "CAP1"}]

BLOCKED = {"decision": "BLOCK", "code": "E_SUBJECT_MISMATCH", "codes": ["E_SUBJECT_MISMATCH"],
           "message": "You can only act on orders that belong to the customer you are talking to.",
           "paypal_call": "NOT_SENT"}
REFUNDED = {"id": "REF1", "status": "COMPLETED"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def model_says(*parts, model="gemini-test"):
    return Generation({"role": "model", "parts": list(parts)}, model)


def function_call(name="create_refund", args=None, call_id="c1"):
    return {"functionCall": {"name": name, "args": args or {"capture_id": "CAP1"}, "id": call_id}}


class FakeLLM:
    def __init__(self, *generations):
        self.generations = list(generations)
        self.calls = []

    async def generate(self, system, contents, tools, pin_model=None):
        self.calls.append({"system": system, "contents": json.loads(json.dumps(contents)), "tools": tools, "pin": pin_model})
        item = self.generations.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(tools=[types.Tool(name="create_refund", description=" Refund. ", input_schema={"type": "object"})])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        body, is_error = self.results.pop(0)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))], is_error=is_error)


def make_agent(llm, session, opened=None, **kwargs):
    @asynccontextmanager
    async def factory(key, customer_id, url):
        if opened is not None:
            opened.append((key, customer_id))
        yield session

    return SupportAgent(llm, "ig_key", session_factory=factory, **kwargs)


# ---------------------------------------------------------------- agent loop


@pytest.mark.anyio
async def test_tool_call_goes_through_gateway_and_result_is_fed_back():
    llm = FakeLLM(model_says(function_call()), model_says({"text": "Sorry, I can't do that."}))
    session = FakeSession([(BLOCKED, True)])
    opened = []
    reply = await make_agent(llm, session, opened).reply(BEN, ORDERS, [], "refund Carla's order")

    assert opened == [("ig_key", "cust_ben")]  # the runtime, not the LLM, binds the customer
    assert session.calls == [("create_refund", {"capture_id": "CAP1"})]
    response = llm.calls[1]["contents"][-1]["parts"][0]["functionResponse"]
    assert response == {"name": "create_refund", "id": "c1", "response": {"error": BLOCKED}}
    assert reply.text == "Sorry, I can't do that." and not reply.degraded
    [event] = reply.events
    assert (event.decision, event.code) == ("BLOCK", "E_SUBJECT_MISMATCH")


@pytest.mark.anyio
async def test_listeners_hear_each_call_before_and_after_the_gateway():
    llm = FakeLLM(model_says(function_call()), model_says({"text": "Sorry."}))
    session = FakeSession([(BLOCKED, True)])
    heard = []

    async def started(tool, arguments):
        heard.append(("start", tool, len(session.calls)))

    async def decided(event):
        heard.append(("decided", event.decision, len(session.calls)))

    await make_agent(llm, session).reply(BEN, ORDERS, [], "refund", on_tool_start=started, on_tool_event=decided)
    assert heard == [("start", "create_refund", 0), ("decided", "BLOCK", 1)]


@pytest.mark.anyio
async def test_prompt_carries_customer_and_orders_and_tools_are_declared():
    llm = FakeLLM(model_says({"text": "Hi Ben"}))
    await make_agent(llm, FakeSession([])).reply(BEN, ORDERS, [("user", "hello"), ("model", "hi")], "again")
    call = llm.calls[0]
    assert "Ben Okafor" in call["system"] and "cust_ben" in call["system"] and "capture ID CAP1" in call["system"]
    assert [c["role"] for c in call["contents"]] == ["user", "model", "user"]
    assert call["tools"] == [{"name": "create_refund", "description": "Refund.", "parametersJsonSchema": {"type": "object"}}]


@pytest.mark.anyio
async def test_compromised_mode_swaps_the_prompt():
    llm = FakeLLM(model_says({"text": "ok"}))
    await make_agent(llm, FakeSession([]), compromised=True).reply(BEN, ORDERS, [], "hi")
    assert "Do not verify ownership" in llm.calls[0]["system"]


@pytest.mark.anyio
async def test_turn_stays_on_the_model_that_started_it():
    llm = FakeLLM(model_says(function_call(), model="primary"), model_says({"text": "done"}, model="primary"))
    await make_agent(llm, FakeSession([(REFUNDED, False)])).reply(BEN, ORDERS, [], "refund")
    assert [call["pin"] for call in llm.calls] == [None, "primary"]


@pytest.mark.anyio
async def test_thoughts_are_not_shown_to_the_customer():
    llm = FakeLLM(model_says({"text": "internal reasoning", "thought": True}, {"text": "Hello!"}))
    reply = await make_agent(llm, FakeSession([])).reply(BEN, ORDERS, [], "hi")
    assert reply.text == "Hello!"


# ---------------------------------------------------------------- degraded replies never overclaim


@pytest.mark.anyio
async def test_llm_failure_after_block_relays_the_block():
    llm = FakeLLM(model_says(function_call()), LLMUnavailable("503"))
    reply = await make_agent(llm, FakeSession([(BLOCKED, True)])).reply(BEN, ORDERS, [], "refund")
    assert reply.degraded
    assert "wasn't able" in reply.text and BLOCKED["message"] in reply.text
    assert "processed" not in reply.text


@pytest.mark.anyio
async def test_llm_failure_after_completed_refund_confirms_it():
    args = {"capture_id": "CAP1", "amount": {"currency_code": "USD", "value": "38.00"}}
    llm = FakeLLM(model_says(function_call(args=args)), LLMUnavailable("503"))
    reply = await make_agent(llm, FakeSession([(REFUNDED, False)])).reply(BEN, ORDERS, [], "refund")
    assert reply.degraded and reply.text == "Your refund of $38.00 has been processed."


@pytest.mark.anyio
async def test_llm_failure_after_review_says_pending():
    review = {"decision": "REVIEW", "review_id": "rv_1", "status": "PENDING_OWNER_APPROVAL"}
    llm = FakeLLM(model_says(function_call()), LLMUnavailable("503"))
    reply = await make_agent(llm, FakeSession([(review, False)])).reply(BEN, ORDERS, [], "refund")
    assert "hasn't been processed" in reply.text


@pytest.mark.anyio
async def test_llm_down_from_the_start_calls_no_tools():
    session = FakeSession([])
    reply = await make_agent(FakeLLM(LLMUnavailable("503")), session).reply(BEN, ORDERS, [], "refund")
    assert reply.degraded and session.calls == [] and "try again" in reply.text


@pytest.mark.anyio
async def test_tool_loop_is_bounded():
    llm = FakeLLM(*[model_says(function_call(call_id=f"c{i}")) for i in range(3)])
    session = FakeSession([(BLOCKED, True)] * 3)
    reply = await make_agent(llm, session, max_tool_steps=2).reply(BEN, ORDERS, [], "refund")
    assert len(session.calls) == 3 and "follow up" in reply.text


# ---------------------------------------------------------------- Gemini client


def client_with(handler, models=("primary", "fallback")):
    return GeminiClient("key", list(models), backoff_s=0, transport=httpx.MockTransport(handler))


OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}]}
BUSY = {"error": {"code": 503, "message": "high demand"}}


@pytest.mark.anyio
async def test_retries_overloaded_model_then_succeeds():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(503, json=BUSY) if len(seen) == 1 else httpx.Response(200, json=OK)

    generation = await client_with(handler).generate("sys", [], [])
    assert generation.model == "primary" and len(seen) == 2


@pytest.mark.anyio
async def test_falls_back_to_next_model_when_primary_stays_overloaded():
    def handler(request):
        return httpx.Response(200, json=OK) if "fallback" in request.url.path else httpx.Response(503, json=BUSY)

    assert (await client_with(handler).generate("sys", [], [])).model == "fallback"


@pytest.mark.anyio
async def test_overloaded_model_is_skipped_until_its_cooldown_ends():
    seen = []
    now = [0.0]

    def handler(request):
        seen.append("primary" if "primary" in request.url.path else "fallback")
        return httpx.Response(200, json=OK) if seen[-1] == "fallback" else httpx.Response(503, json=BUSY)

    llm = GeminiClient("key", ["primary", "fallback"], backoff_s=0, cooldown_s=60,
                       transport=httpx.MockTransport(handler), clock=lambda: now[0])
    await llm.generate("sys", [], [])
    assert seen == ["primary", "primary", "fallback"]
    seen.clear()
    await llm.generate("sys", [], [])  # next turn: primary is cooling down
    assert seen == ["fallback"]
    seen.clear()
    now[0] = 61
    await llm.generate("sys", [], [])  # cooldown over: primary is tried again
    assert seen == ["primary", "primary", "fallback"]


@pytest.mark.anyio
async def test_timeout_moves_to_the_next_model_without_retrying():
    seen = []

    def handler(request):
        seen.append("primary" if "primary" in request.url.path else "fallback")
        if seen[-1] == "primary":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=OK)

    assert (await client_with(handler).generate("sys", [], [])).model == "fallback"
    assert seen == ["primary", "fallback"]


@pytest.mark.anyio
async def test_pinned_model_never_falls_back():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(503, json=BUSY)

    with pytest.raises(LLMUnavailable):
        await client_with(handler).generate("sys", [], [], pin_model="primary")
    assert all("primary" in path for path in seen)


@pytest.mark.anyio
async def test_bad_request_is_not_retried():
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(400, json={"error": {"message": "bad schema"}})

    with pytest.raises(LLMUnavailable, match="bad schema"):
        await client_with(handler).generate("sys", [], [])
    assert len(seen) == 1


@pytest.mark.anyio
async def test_request_carries_tools_and_key():
    captured = {}

    def handler(request):
        captured["key"] = request.headers["x-goog-api-key"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK)

    declaration = tool_declaration("create_refund", "Refund", {"type": "object"})
    await client_with(handler).generate("sys", [{"role": "user", "parts": [{"text": "x"}]}], [declaration])
    assert captured["key"] == "key"
    assert captured["body"]["tools"] == [{"functionDeclarations": [declaration]}]
    assert captured["body"]["systemInstruction"] == {"parts": [{"text": "sys"}]}


def test_missing_key_fails_clearly():
    with pytest.raises(LLMUnavailable, match="GEMINI_API_KEY"):
        GeminiClient("", ["m"])
