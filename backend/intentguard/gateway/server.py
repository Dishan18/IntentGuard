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
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server

from intentguard import demo
from intentguard.config import get_settings
from intentguard.agents.llm import GeminiClient, LLMUnavailable
from intentguard.gateway import registry
from intentguard.gateway.downstream import DownstreamUnavailable, PayPalDownstream
from intentguard.gateway.facts import FactsService
from intentguard.gateway.ledger import Ledger, Reviews
from intentguard.gateway.mandates import MandateStore
from intentguard.gateway.reconcile import Reconciler
from intentguard.paypal.auth import TokenProvider
from intentguard.paypal.rest import PayPalRest
from intentguard.policy.engine import evaluate
from intentguard.policy.models import (
    AgentView,
    CaptureFacts,
    FactsFailure,
    CheckResult,
    Decision,
    EvaluationInput,
    LedgerSnapshot,
    Mandate,
    Outcome,
)

log = logging.getLogger("intentguard.gateway")

CUSTOMER_HEADER = "x-intentguard-customer"
AGENT_SCOPE_KEY = "intentguard.agent"

# Block codes where the agent can fix its own request and try again. Limit and binding codes are
# deliberately not retryable: the agent should hand off to a person, not hunt for a smaller amount.
RETRYABLE_CODES = {
    "E_INVALID_REQUEST", "E_CURRENCY_NOT_ALLOWED", "E_UNKNOWN_CAPTURE", "E_UNKNOWN_ORDER", "E_UNKNOWN_REFUND",
    "E_UNKNOWN_INVOICE", "E_FACTS_UNAVAILABLE",
}

# Red-team identities (gateway/redteam.py): decided exactly like the agent they stand in for, with
# real PayPal facts and the same mandate, but nothing is ever forwarded, reserved or queued.
DRY_RUN_AGENTS = {"redteam-agent": "support-agent"}
# Groups a red-team attack's calls, so later calls see what earlier ones would have done.
SCENARIO_HEADER = "x-intentguard-scenario"

# Calls that move money or reach customers. An identical one within the window, after one that was
# allowed or queued, is a replay (a retry loop, a resent request) and is refused: PayPal doesn't
# deduplicate refunds an agent asks for, so the gateway must.
REPLAY_GUARDED_TOOLS = {"create_refund", "create_invoice", "send_invoice", "send_invoice_reminder"}
REPLAY_WINDOW = timedelta(minutes=10)

# How long the gateway waits for PayPal on a forwarded call before recording OUTCOME_UNKNOWN.
FORWARD_TIMEOUT_S = 60.0

# How often the poller checks refunds whose outcome isn't final (see gateway/reconcile.py).
RECONCILE_INTERVAL_S = float(os.getenv("RECONCILE_INTERVAL_S", "60"))


@dataclass
class Shadow:
    """What one dry-run attack would have refunded so far. Overlaid on the real ledger and PayPal
    facts for that attack's later calls, so "refund $40, then another $40" is judged as a whole."""

    refunds: list[tuple[str | None, str, Decimal]] = field(default_factory=list)  # customer, capture, amount

    def snapshot(self, real: LedgerSnapshot, customer_id: str | None) -> LedgerSnapshot:
        return LedgerSnapshot(
            customer_refunds_in_window=real.customer_refunds_in_window
            + sum(1 for customer, _, _ in self.refunds if customer == customer_id),
            refunded_today=real.refunded_today + sum((amount for _, _, amount in self.refunds), Decimal("0")),
        )

    def facts(self, facts):
        if not isinstance(facts, CaptureFacts):
            return facts
        extra = sum((amount for _, capture, amount in self.refunds if capture == facts.capture_id), Decimal("0"))
        return replace(facts, already_refunded=facts.already_refunded + extra)


@dataclass
class GatewayState:
    db: sqlite3.Connection
    downstream: PayPalDownstream
    facts: FactsService
    mandates: Callable[[str], Mandate | None] = demo.MANDATES.get
    reconciler: Reconciler | None = None
    webhook_id: str = ""
    compiler_llm: GeminiClient | None = None  # drafts mandates from the owner's text; None without a key
    shadows: dict[str, Shadow] = field(default_factory=dict)  # per red-team attack
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


def paypal_failed(is_error: bool, result_text: str) -> bool:
    """PayPal's toolkit reports API errors as an ordinary result, `{"ok": false, "status": 403, ...}`,
    not as an MCP error, so both have to be checked."""
    if is_error:
        return True
    try:
        body = json.loads(result_text)
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("ok") is False


def _refund_fields(result_text: str) -> tuple[str | None, str | None]:
    """PayPal's refund ID and status from a create_refund result."""
    try:
        body = json.loads(result_text)
        return body.get("id"), body.get("status")
    except (ValueError, AttributeError):
        return None, None


async def read_facts(state: "GatewayState", tool: str, arguments: dict):
    """Facts from PayPal for a call. Whatever goes wrong (PayPal down, an HTML error page, an
    unexpected shape, missing credentials) becomes a facts failure, so the call is blocked and
    audited rather than crashing with no trace."""
    try:
        return await anyio.to_thread.run_sync(state.facts.for_call, tool, arguments)
    except Exception as error:
        log.exception("reading facts for %s failed", tool)
        return FactsFailure(not_found=False, detail=f"couldn't read facts from PayPal ({type(error).__name__})")


def evaluate_locked(
    state: GatewayState,
    agent: registry.Agent,
    mandate: Mandate | None,
    tool: str,
    arguments: dict,
    customer_id: str | None,
    facts,
    now: datetime,
    shadow: Shadow | None = None,
    duplicate_of: str | None = None,
) -> Decision:
    """Evaluate a call inside an open ledger transaction, against an up-to-date ledger snapshot."""
    snapshot = LedgerSnapshot()
    if tool == "create_refund" and mandate is not None and mandate.refunds is not None:
        state.reviews.expire_stale(now)  # an abandoned review stops counting toward the customer's limit
        snapshot = state.ledger.snapshot(mandate, customer_id, now)
        if shadow is not None:
            snapshot = shadow.snapshot(snapshot, customer_id)
    if duplicate_of:
        snapshot = replace(snapshot, duplicate_of=duplicate_of)
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
    mandate: Mandate | None = None,
    outcome: str | None = None,
) -> None:
    """Write the audit row, with the mandate version it was decided under. Called before anything
    is forwarded to PayPal."""
    state.db.execute(
        "INSERT INTO tool_calls (id, agent_id, customer_id, tool, arguments, decision, reason_code, codes, checks,"
        " resolved_amount, review_id, outcome, mandate_id, mandate_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            agent_id,
            customer_id,
            tool,
            _canonical(arguments),
            decision.outcome.value,
            decision.code,
            json.dumps(list(decision.codes)),
            json.dumps([check.__dict__ | {"result": check.result.value} for check in decision.checks]),
            None if decision.resolved_amount is None else str(decision.resolved_amount),
            review_id,
            outcome or {"ALLOW": "PENDING", "REVIEW": "AWAITING_REVIEW", "BLOCK": "NOT_SENT"}[decision.outcome.value],
            mandate.id if mandate else None,
            mandate.version if mandate else None,
        ),
    )


def find_replay(state: "GatewayState", agent_id: str, customer_id: str | None, tool: str, arguments: dict,
                now: datetime) -> str | None:
    """An identical request from the same agent for the same customer that was allowed or queued within
    the window. Called inside the ledger transaction, so two copies sent at once can't both pass.
    A request that was blocked, or that PayPal rejected, may be made again."""
    if tool not in REPLAY_GUARDED_TOOLS:
        return None
    row = state.db.execute(
        "SELECT id FROM tool_calls WHERE agent_id = ? AND IFNULL(customer_id, '') = IFNULL(?, '') AND tool = ?"
        " AND arguments = ? AND created_at >= ?"
        " AND ((decision = 'ALLOW' AND outcome IN ('PENDING', 'OK', 'OUTCOME_UNKNOWN'))"
        "   OR (decision = 'REVIEW' AND outcome IN ('AWAITING_REVIEW', 'REVIEW_APPROVED')))"
        " ORDER BY created_at DESC LIMIT 1",
        (agent_id, customer_id, tool, _canonical(arguments), (now - REPLAY_WINDOW).isoformat(timespec="milliseconds")),
    ).fetchone()
    return row["id"] if row else None


def _canonical(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True)


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
    stands_in_for = DRY_RUN_AGENTS.get(agent.id)
    mandate = state.mandates(stands_in_for or agent.id)

    # Only fetch facts for calls that could be allowed; never spend a PayPal call on a disallowed tool.
    facts = None
    if tool in agent.allowed_tools and tool in state.downstream.tools:
        facts = await read_facts(state, tool, arguments)

    if stands_in_for:
        scenario = ctx.request.headers.get(SCENARIO_HEADER) or call_id
        return dry_run(state, state.shadows.setdefault(scenario, Shadow()), agent, stands_in_for, mandate, tool,
                       arguments, customer_id, facts, call_id)

    now = state.clock()
    review_id = None
    with state.ledger.transaction():
        replay_of = find_replay(state, agent.id, customer_id, tool, arguments, now)
        decision = evaluate_locked(state, agent, mandate, tool, arguments, customer_id, facts, now, duplicate_of=replay_of)
        forward_args = _forward_arguments(tool, arguments, decision, mandate) if mandate else arguments

        if decision.outcome is Outcome.REVIEW:
            review_id = state.reviews.create(
                call_id, mandate, agent.id, customer_id, tool, forward_args, decision.resolved_amount, list(decision.codes), now
            )
        if decision.outcome is Outcome.ALLOW and tool == "create_refund":
            state.ledger.reserve(
                call_id, mandate, agent.id, customer_id, arguments["capture_id"], decision.resolved_amount, now
            )
        record_call(state, call_id, agent.id, customer_id, tool, arguments, decision, review_id, mandate)

    if decision.outcome is Outcome.BLOCK:
        return block_result(call_id, decision)
    if decision.outcome is Outcome.REVIEW:
        return review_result(call_id, review_id, decision)
    return await forward(state, call_id, tool, forward_args)


def dry_run(
    state: GatewayState,
    shadow: Shadow,
    agent: registry.Agent,
    stands_in_for: str,
    mandate: Mandate | None,
    tool: str,
    arguments: dict,
    customer_id: str | None,
    facts,
    call_id: str,
) -> types.CallToolResult:
    """A red-team call: the real decision, the same answer the agent would get, and no side effects.
    Audited with outcome DRY_RUN; the console keeps these out of the owner's activity and totals."""
    now = state.clock()
    with state.ledger.transaction():
        # Judged as the agent it stands in for (whose mandate it is), with its own allowlist.
        as_agent = replace(agent, id=stands_in_for)
        decision = evaluate_locked(state, as_agent, mandate, tool, arguments, customer_id, shadow.facts(facts), now, shadow)
        record_call(state, call_id, agent.id, customer_id, tool, arguments, decision, None, mandate, outcome="DRY_RUN")
    if decision.outcome is Outcome.BLOCK:
        return block_result(call_id, decision)
    if decision.outcome is Outcome.REVIEW:
        return review_result(call_id, "rv_dry_run", decision)
    if tool == "create_refund" and decision.resolved_amount is not None:
        shadow.refunds.append((customer_id, arguments.get("capture_id", ""), decision.resolved_amount))
    return _json_result(
        {
            "id": f"DRYRUN{call_id[-10:].upper()}",
            "status": "COMPLETED",
            "dry_run": True,
            **(
                {"amount": {"currency_code": mandate.currency, "value": f"{decision.resolved_amount:.2f}"}}
                if decision.resolved_amount is not None and mandate
                else {}
            ),
            "note": "Red-team dry run: IntentGuard would have allowed this. Nothing was sent to PayPal.",
        },
        is_error=False,
    )


async def forward(state: GatewayState, call_id: str, tool: str, arguments: dict) -> types.CallToolResult:
    """Send an approved call to PayPal and settle the audit row and any reservation.

    A refund carries the call ID as its PayPal `invoice_id`, so if the outcome is ever unclear the
    reconciler can find exactly the refund this call created (or be sure there isn't one).
    """
    if tool == "create_refund":
        arguments = {**arguments, "invoice_id": call_id}

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
    failed = paypal_failed(result.is_error, text)
    record(
        forwarded=1,
        outcome="PAYPAL_ERROR" if failed else "OK",
        result=text,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if failed:
        state.ledger.release(call_id, state.clock())
    else:
        refund_id, paypal_status = _refund_fields(text)
        state.ledger.commit(call_id, refund_id, state.clock(), paypal_status)
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
        paypal = PayPalRest(settings, tokens)
        facts = FactsService(paypal)
        mandates = MandateStore(db)
        mandates.seed(demo.MANDATES.values(), datetime.now(UTC))
        # A red-team run can't survive a restart; don't let it block the next one.
        db.execute("UPDATE redteam_runs SET status = 'FAILED', error = 'interrupted: the gateway restarted'"
                   " WHERE status = 'RUNNING'")
        async with anyio.create_task_group() as task_group:
            await task_group.start(downstream.run)
            state = GatewayState(
                db=db, downstream=downstream, facts=facts, mandates=mandates.active, webhook_id=settings.paypal_webhook_id
            )
            state.reconciler = Reconciler(db, paypal, facts, state.clock)
            try:
                state.compiler_llm = GeminiClient.from_env()
            except LLMUnavailable:
                state.compiler_llm = None
            task_group.start_soon(state.reconciler.poll, RECONCILE_INTERVAL_S)
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
