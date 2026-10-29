"""IntentGuard MCP gateway.

Agents connect here (streamable HTTP, `/mcp`) instead of to PayPal. For each tool call:

1. the agent is identified by its API key and the tool checked against its allowlist;
2. facts are read from PayPal by the gateway itself (outside any lock: it's network I/O);
3. in one SQLite transaction: ledger snapshot, policy evaluation, audit row, and, for an
   allowed refund, a reservation of the amount;
4. only ALLOW is forwarded to PayPal, with the amount the policy checked filled in;
   REVIEW is queued for the owner; BLOCK returns a structured error. Neither reaches PayPal.

The runtime that hosts an agent (not the LLM) sends `X-IntentGuard-Customer` with the
authenticated customer the conversation belongs to; the policy binds actions to it.
"""

import json
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anyio
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server

from intentguard import demo
from intentguard.config import get_settings
from intentguard.gateway import registry
from intentguard.gateway.downstream import DownstreamUnavailable, PayPalDownstream
from intentguard.gateway.facts import FactsService
from intentguard.gateway.ledger import Ledger, Reviews
from intentguard.paypal.auth import TokenProvider
from intentguard.paypal.rest import PayPalRest
from intentguard.policy.engine import evaluate
from intentguard.policy.models import (
    AgentView,
    CheckResult,
    Decision,
    EvaluationInput,
    LedgerSnapshot,
    Mandate,
    Outcome,
)

CUSTOMER_HEADER = "x-intentguard-customer"
AGENT_SCOPE_KEY = "intentguard.agent"

# Block codes where the agent can fix its own request and try again. Limit and binding codes are
# deliberately not retryable: the agent should hand off to a person, not hunt for a smaller amount.
RETRYABLE_CODES = {"E_INVALID_REQUEST", "E_CURRENCY_NOT_ALLOWED", "E_UNKNOWN_CAPTURE", "E_UNKNOWN_INVOICE", "E_FACTS_UNAVAILABLE"}

# How long the gateway waits for PayPal on a forwarded call before recording OUTCOME_UNKNOWN.
FORWARD_TIMEOUT_S = 60.0


@dataclass
class GatewayState:
    db: sqlite3.Connection
    downstream: PayPalDownstream
    facts: FactsService
    mandates: Callable[[str], Mandate | None] = demo.MANDATES.get
    known_customer_emails: frozenset[str] = demo.KNOWN_CUSTOMER_EMAILS
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.db)

    @property
    def reviews(self) -> Reviews:
        return Reviews(self.db)


def _agent(ctx: ServerRequestContext) -> registry.Agent:
    # Set by AuthMiddleware (gateway/app.py); requests without a valid key never reach the handlers.
    return ctx.request.scope[AGENT_SCOPE_KEY]


def _customer(ctx: ServerRequestContext) -> str | None:
    return ctx.request.headers.get(CUSTOMER_HEADER)


def _json_result(body: dict, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))], is_error=is_error)


def _reasons(decision: Decision, result: CheckResult) -> list[dict]:
    return [
        {"rule": check.rule, "code": check.code, "detail": check.detail}
        for check in decision.checks
        if check.result is result
    ]


def block_result(call_id: str, decision: Decision) -> types.CallToolResult:
    return _json_result(
        {
            "decision": "BLOCK",
            "code": decision.code,
            "codes": [code for code in decision.codes if code.startswith("E_")],
            "message": decision.agent_hint,
            "reasons": _reasons(decision, CheckResult.FAIL),
            "retryable": decision.code in RETRYABLE_CODES,
            "paypal_call": "NOT_SENT",
            "call_id": call_id,
        },
        is_error=True,
    )


def review_result(call_id: str, review_id: str, decision: Decision) -> types.CallToolResult:
    # Not an error: the request is valid and waiting for the owner. It hasn't happened yet.
    return _json_result(
        {
            "decision": "REVIEW",
            "status": "PENDING_OWNER_APPROVAL",
            "review_id": review_id,
            "message": decision.agent_hint,
            "reasons": _reasons(decision, CheckResult.REVIEW),
            "paypal_call": "NOT_SENT_YET",
            "call_id": call_id,
        },
        is_error=False,
    )


def _error_result(code: str, message: str, call_id: str) -> types.CallToolResult:
    return _json_result({"decision": "BLOCK", "code": code, "message": message, "call_id": call_id}, is_error=True)


def _result_text(result: types.CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, types.TextContent))


def _forward_arguments(tool: str, arguments: dict, decision: Decision, mandate: Mandate) -> dict:
    """What PayPal receives. A refund always carries the exact amount the policy approved, so an
    omitted amount can't turn into a different "full refund" if the capture changes in between."""
    if tool == "create_refund" and decision.resolved_amount is not None:
        return {
            **arguments,
            "amount": {"currency_code": mandate.currency, "value": f"{decision.resolved_amount:.2f}"},
        }
    return arguments


def _refund_id(result_text: str) -> str | None:
    try:
        return json.loads(result_text).get("id")
    except (ValueError, AttributeError):
        return None


def evaluate_locked(
    state: GatewayState,
    agent: registry.Agent,
    mandate: Mandate | None,
    tool: str,
    arguments: dict,
    customer_id: str | None,
    facts,
    now: datetime,
) -> Decision:
    """Evaluate a call inside an open ledger transaction, against an up-to-date ledger snapshot."""
    snapshot = LedgerSnapshot()
    if tool == "create_refund" and mandate is not None and mandate.refunds is not None:
        state.reviews.expire_stale(now)  # an abandoned review stops counting toward the customer's limit
        snapshot = state.ledger.snapshot(mandate, customer_id, now)
    decision = evaluate(
        EvaluationInput(
            agent=AgentView(agent.id, agent.active, agent.allowed_tools),
            mandate=mandate,
            tool=tool,
            arguments=arguments,
            session_customer=customer_id,
            facts=facts,
            ledger=snapshot,
            known_customer_emails=state.known_customer_emails,
            now=now,
        )
    )
    if decision.outcome is Outcome.ALLOW and tool not in state.downstream.tools:
        decision = Decision(Outcome.BLOCK, decision.checks, "E_TOOL_NOT_ALLOWED", codes=("E_TOOL_NOT_ALLOWED",))
    return decision


def record_call(
    state: GatewayState,
    call_id: str,
    agent_id: str,
    customer_id: str | None,
    tool: str,
    arguments: dict,
    decision: Decision,
    review_id: str | None,
) -> None:
    """Write the audit row. Called before anything is forwarded to PayPal."""
    state.db.execute(
        "INSERT INTO tool_calls (id, agent_id, customer_id, tool, arguments, decision, reason_code, codes, checks,"
        " resolved_amount, review_id, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            agent_id,
            customer_id,
            tool,
            json.dumps(arguments),
            decision.outcome.value,
            decision.code,
            json.dumps(list(decision.codes)),
            json.dumps([check.__dict__ | {"result": check.result.value} for check in decision.checks]),
            None if decision.resolved_amount is None else str(decision.resolved_amount),
            review_id,
            {"ALLOW": "PENDING", "REVIEW": "AWAITING_REVIEW", "BLOCK": "NOT_SENT"}[decision.outcome.value],
        ),
    )


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


async def list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    state: GatewayState = ctx.lifespan_context
    agent = _agent(ctx)
    tools = [tool for name, tool in sorted(state.downstream.tools.items()) if name in agent.allowed_tools]
    return types.ListToolsResult(tools=tools)


async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    state: GatewayState = ctx.lifespan_context
    agent = _agent(ctx)
    customer_id = _customer(ctx)
    tool = params.name
    arguments = dict(params.arguments or {})
    call_id = new_call_id()
    mandate = state.mandates(agent.id)

    # Only fetch facts for calls that could be allowed; never spend a PayPal call on a disallowed tool.
    facts = None
    if tool in agent.allowed_tools and tool in state.downstream.tools:
        facts = await anyio.to_thread.run_sync(state.facts.for_call, tool, arguments)

    now = state.clock()
    review_id = None
    with state.ledger.transaction():
        decision = evaluate_locked(state, agent, mandate, tool, arguments, customer_id, facts, now)
        forward_args = _forward_arguments(tool, arguments, decision, mandate) if mandate else arguments

        if decision.outcome is Outcome.REVIEW:
            review_id = state.reviews.create(
                call_id, mandate, agent.id, customer_id, tool, forward_args, decision.resolved_amount, list(decision.codes), now
            )
        if decision.outcome is Outcome.ALLOW and tool == "create_refund":
            state.ledger.reserve(
                call_id, mandate, agent.id, customer_id, arguments["capture_id"], decision.resolved_amount, now
            )
        record_call(state, call_id, agent.id, customer_id, tool, arguments, decision, review_id)

    if decision.outcome is Outcome.BLOCK:
        return block_result(call_id, decision)
    if decision.outcome is Outcome.REVIEW:
        return review_result(call_id, review_id, decision)
    return await forward(state, call_id, tool, forward_args)


async def forward(state: GatewayState, call_id: str, tool: str, arguments: dict) -> types.CallToolResult:
    """Send an approved call to PayPal and settle the audit row and any reservation."""

    def record(**fields) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        state.db.execute(f"UPDATE tool_calls SET {columns} WHERE id = ?", (*fields.values(), call_id))

    started = time.monotonic()
    try:
        # Shielded: if the agent disconnects mid-call, PayPal may still act on the request, so the
        # gateway waits for the outcome and records it. The timeout turns a hang into OUTCOME_UNKNOWN.
        with anyio.CancelScope(shield=True), anyio.fail_after(FORWARD_TIMEOUT_S):
            result = await state.downstream.call_tool(tool, arguments)
    except DownstreamUnavailable as error:
        record(outcome="NOT_SENT", result=str(error))
        state.ledger.release(call_id, state.clock())
        return _error_result("E_DOWNSTREAM_UNAVAILABLE", "PayPal is unreachable; nothing was sent.", call_id)
    except Exception as error:
        # The request may have reached PayPal. Never retry blindly; reconcile instead.
        record(forwarded=1, outcome="OUTCOME_UNKNOWN", result=repr(error))
        state.ledger.mark_unknown(call_id, state.clock())
        raise

    text = _result_text(result)
    record(
        forwarded=1,
        outcome="PAYPAL_ERROR" if result.is_error else "OK",
        result=text,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if result.is_error:
        state.ledger.release(call_id, state.clock())
    else:
        state.ledger.commit(call_id, _refund_id(text), state.clock())
    return result


def build_server(
    db: sqlite3.Connection, on_state: Callable[[GatewayState | None], None] = lambda _: None
) -> Server[GatewayState]:
    """`on_state` is told the running state (and None at shutdown), for routes outside MCP."""

    @asynccontextmanager
    async def lifespan(_: Server) -> AsyncIterator[GatewayState]:
        settings = get_settings()
        tokens = TokenProvider(settings)
        downstream = PayPalDownstream(settings, tokens)
        async with anyio.create_task_group() as task_group:
            await task_group.start(downstream.run)
            state = GatewayState(db=db, downstream=downstream, facts=FactsService(PayPalRest(settings, tokens)))
            on_state(state)
            try:
                yield state
            finally:
                on_state(None)
                task_group.cancel_scope.cancel()

    return Server(
        "intentguard",
        version="0.1.0",
        instructions=(
            "IntentGuard policy gateway in front of PayPal. A blocked call returns JSON with decision=BLOCK, "
            "a code and a message to relay to the customer. decision=REVIEW means the owner must approve; "
            "the action has not happened yet."
        ),
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
