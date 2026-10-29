"""The store owner's side of REVIEW: list held requests, approve or reject them.

Approving executes the *stored* request, the exact call (with the resolved amount) that was held,
never a new one from the agent. Before it is sent, it goes through the policy again with fresh
PayPal facts and the current ledger, in one `BEGIN IMMEDIATE` transaction:

- the owner's approval covers the review reasons they were shown, and nothing else;
- any hard rule still applies (the capture may have been refunded meanwhile, the daily cap used up,
  the agent revoked), and a failed re-check means nothing is sent.

Owner routes authenticate with an owner key, which is separate from agent keys: an agent can
never approve its own requests.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from intentguard.gateway import registry
from intentguard.gateway.ledger import REVIEW_TTL
from intentguard.gateway.server import (
    GatewayState,
    _forward_arguments,
    evaluate_locked,
    forward,
    new_call_id,
    record_call,
)
from intentguard.policy.models import CheckResult, Decision, Outcome

OWNER_SCOPE_KEY = "intentguard.owner"
MAX_NOTE_CHARS = 500


def _iso_to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _review_view(db, row) -> dict:
    call = db.execute("SELECT checks FROM tool_calls WHERE id = ?", (row["call_id"],)).fetchone()
    checks = json.loads(call["checks"]) if call and call["checks"] else []
    execution = None
    if row["execution_call_id"]:
        executed = db.execute(
            "SELECT decision, codes, outcome, result FROM tool_calls WHERE id = ?", (row["execution_call_id"],)
        ).fetchone()
        if executed:
            execution = {
                "call_id": row["execution_call_id"],
                "decision": executed["decision"],
                "codes": json.loads(executed["codes"] or "[]"),
                "outcome": executed["outcome"],
            }
    return {
        "review_id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "agent_id": row["agent_id"],
        "customer_id": row["customer_id"],
        "tool": row["tool"],
        "arguments": json.loads(row["arguments"]),
        "amount": row["amount"],
        "codes": json.loads(row["codes"]),
        "reasons": [
            {"rule": check["rule"], "code": check["code"], "detail": check["detail"]}
            for check in checks
            if check["result"] == CheckResult.REVIEW.value
        ],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
        "note": row["note"],
        "execution": execution,
    }


def list_reviews(state: GatewayState, status: str | None = None) -> list[dict]:
    with state.ledger.transaction():
        state.reviews.expire_stale(state.clock())
    query, params = "SELECT * FROM reviews", ()
    if status:
        query, params = query + " WHERE status = ?", (status,)
    rows = state.db.execute(query + " ORDER BY created_at DESC LIMIT 200", params).fetchall()
    return [_review_view(state.db, row) for row in rows]


def _not_pending(review_id: str, status: str) -> tuple[int, dict]:
    return 409, {"error": "E_REVIEW_NOT_PENDING", "review_id": review_id, "status": status,
                 "message": f"This review is already {status}."}


def _covered_by_approval(decision: Decision, approved_codes: set[str]) -> Decision:
    """The owner approved the review reasons they saw. If the re-check still only needs review for
    those reasons, the approval stands in for them; a new reason means the owner must look again."""
    if decision.outcome is not Outcome.REVIEW:
        return decision
    pending = {check.code for check in decision.checks if check.result is CheckResult.REVIEW}
    if pending <= approved_codes:
        return replace(decision, outcome=Outcome.ALLOW, code=None, agent_hint="")
    return Decision(
        Outcome.BLOCK, decision.checks, "E_REVIEW_CHANGED",
        resolved_amount=decision.resolved_amount,
        agent_hint="The request needs review for a new reason; it was not executed.",
        codes=("E_REVIEW_CHANGED", *sorted(pending - approved_codes)),
    )


async def approve(state: GatewayState, review_id: str, owner: str, note: str | None = None) -> tuple[int, dict]:
    review = state.reviews.get(review_id)
    if review is None:
        return 404, {"error": "E_REVIEW_UNKNOWN", "review_id": review_id}
    if review["status"] != "PENDING":
        return _not_pending(review_id, review["status"])

    tool = review["tool"]
    arguments = json.loads(review["arguments"])
    facts = await anyio.to_thread.run_sync(state.facts.for_call, tool, arguments)  # fresh, not the ones from before
    now = state.clock()
    call_id = new_call_id()

    with state.ledger.transaction():
        review = state.reviews.get(review_id)  # re-read under the write lock: two approvals can't both pass
        if review["status"] != "PENDING":
            return _not_pending(review_id, review["status"])
        if _iso_to_datetime(review["created_at"]) < now - REVIEW_TTL:
            state.reviews.resolve(review_id, "EXPIRED", now, "system")
            return _not_pending(review_id, "EXPIRED")

        # Marked approved first, so it no longer counts as a pending refund in the snapshot below.
        state.reviews.resolve(review_id, "APPROVED", now, owner, note, call_id)
        agent = registry.get(state.db, review["agent_id"])
        mandate = state.mandates(review["agent_id"])
        if agent is None:
            decision = Decision(Outcome.BLOCK, (), "E_AGENT_UNKNOWN", codes=("E_AGENT_UNKNOWN",))
        else:
            decision = evaluate_locked(state, agent, mandate, tool, arguments, review["customer_id"], facts, now)
            decision = _covered_by_approval(decision, set(json.loads(review["codes"])))
        forward_args = _forward_arguments(tool, arguments, decision, mandate) if mandate else arguments

        if decision.outcome is Outcome.BLOCK:
            state.reviews.resolve(review_id, "FAILED", now)
        elif tool == "create_refund":
            state.ledger.reserve(
                call_id, mandate, review["agent_id"], review["customer_id"], arguments["capture_id"],
                decision.resolved_amount, now,
            )
        record_call(state, call_id, review["agent_id"], review["customer_id"], tool, arguments, decision, review_id)
        state.db.execute("UPDATE tool_calls SET outcome = 'REVIEW_APPROVED' WHERE id = ?", (review["call_id"],))

    if decision.outcome is Outcome.BLOCK:
        return 200, {
            "review_id": review_id,
            "status": "FAILED",
            "call_id": call_id,
            "codes": [code for code in decision.codes if code.startswith("E_")],
            "message": decision.agent_hint or "The request no longer passes the owner's rules.",
            "paypal_call": "NOT_SENT",
        }

    result = await forward(state, call_id, tool, forward_args)
    text = "".join(block.text for block in result.content if getattr(block, "text", None))
    try:
        paypal = json.loads(text)
    except ValueError:
        paypal = text
    if result.is_error:
        state.reviews.resolve(review_id, "FAILED", state.clock())
    return 200, {
        "review_id": review_id,
        "status": "FAILED" if result.is_error else "APPROVED",
        "call_id": call_id,
        "paypal_call": "SENT",
        "paypal": paypal,
    }


def reject(state: GatewayState, review_id: str, owner: str, note: str | None = None) -> tuple[int, dict]:
    now = state.clock()
    with state.ledger.transaction():
        review = state.reviews.get(review_id)
        if review is None:
            return 404, {"error": "E_REVIEW_UNKNOWN", "review_id": review_id}
        if review["status"] != "PENDING":
            return _not_pending(review_id, review["status"])
        state.reviews.resolve(review_id, "REJECTED", now, owner, note)
        state.db.execute("UPDATE tool_calls SET outcome = 'REVIEW_REJECTED' WHERE id = ?", (review["call_id"],))
    return 200, {"review_id": review_id, "status": "REJECTED", "paypal_call": "NOT_SENT"}


def routes(get_state: Callable[[], GatewayState | None]) -> list[Route]:
    """HTTP routes for the owner API. `get_state` returns the running GatewayState, or None."""

    def handler(action):
        async def endpoint(request: Request) -> JSONResponse:
            state = get_state()
            if state is None:
                return JSONResponse({"error": "E_NOT_READY", "message": "The gateway is still starting."}, 503)
            status, body = await action(state, request)
            return JSONResponse(body, status)

        return endpoint

    async def note_from(request: Request) -> str | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        note = body.get("note") if isinstance(body, dict) else None
        if not isinstance(note, str) or not note.strip():
            return None
        return note.strip()[:MAX_NOTE_CHARS]

    async def get_reviews(state: GatewayState, request: Request):
        status = request.query_params.get("status")
        return 200, list_reviews(state, status.upper() if status else None)

    async def post_approve(state: GatewayState, request: Request):
        owner_name = request.scope[OWNER_SCOPE_KEY]
        return await approve(state, request.path_params["review_id"], owner_name, await note_from(request))

    async def post_reject(state: GatewayState, request: Request):
        owner_name = request.scope[OWNER_SCOPE_KEY]
        return reject(state, request.path_params["review_id"], owner_name, await note_from(request))

    return [
        Route("/owner/reviews", handler(get_reviews), methods=["GET"]),
        Route("/owner/reviews/{review_id}/approve", handler(post_approve), methods=["POST"]),
        Route("/owner/reviews/{review_id}/reject", handler(post_reject), methods=["POST"]),
    ]
