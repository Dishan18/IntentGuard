"""Read side of the owner console: the decision grid, one decision's full audit trail, today's KPIs,
and a live stream of all three. Mounted under /owner, so it needs the owner key.

    GET /owner/decisions         the latest decisions, newest first (the grid)
    GET /owner/decisions/{id}    one decision: the four audit questions and a timeline
    GET /owner/summary           today's KPIs and each mandate's daily cap usage
    GET /owner/stream            server-sent events: a `snapshot` whenever any of the above changes

Nothing here writes. Decisions on held requests go through gateway/owner.py.
"""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from intentguard import demo
from intentguard.gateway.ledger import Ledger
from intentguard.gateway.mandates import MandateStore
from intentguard.gateway.owner import list_reviews, review_view
from intentguard.policy.models import Mandate

GRID_LIMIT = 500
STREAM_TICK_S = 1.0
KEEPALIVE_S = 15.0

_DECISIONS_SQL = """
SELECT c.*, l.refund_id AS ledger_refund_id, l.status AS ledger_status, l.paypal_status,
       r.status AS review_status
FROM tool_calls c
LEFT JOIN refund_ledger l ON l.call_id = c.id
LEFT JOIN reviews r ON r.id = c.review_id
"""


def _json(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return text


def _amount(row: sqlite3.Row) -> str | None:
    if row["resolved_amount"]:
        return row["resolved_amount"]
    amount = (_json(row["arguments"], {}) or {}).get("amount")
    return amount.get("value") if isinstance(amount, dict) else None


def _paypal_call(row: sqlite3.Row) -> str:
    if row["forwarded"]:
        return "SENT"
    if row["outcome"] == "AWAITING_REVIEW":
        return "WAITING_FOR_OWNER"
    if row["outcome"] in ("REVIEW_APPROVED", "REVIEW_REJECTED"):
        return "SEE_REVIEW" if row["outcome"] == "REVIEW_APPROVED" else "NOT_SENT"
    return "NOT_SENT"


def _paypal_id(row: sqlite3.Row) -> str | None:
    if row["ledger_refund_id"]:
        return row["ledger_refund_id"]
    result = _json(row["result"])
    return result.get("id") if isinstance(result, dict) and row["outcome"] == "OK" else None


def _status(row: sqlite3.Row) -> str:
    """One word for the grid's status column: what finally happened."""
    if row["paypal_status"]:
        return row["paypal_status"]
    return {
        "OK": "DONE",
        "PENDING": "IN_FLIGHT",
        "AWAITING_REVIEW": "AWAITING_OWNER",
        "REVIEW_APPROVED": "APPROVED",
        "REVIEW_REJECTED": "REJECTED",
        "NOT_SENT": "STOPPED",
        "PAYPAL_ERROR": "PAYPAL_ERROR",
        "OUTCOME_UNKNOWN": "UNKNOWN",
    }.get(row["outcome"], row["outcome"])


def decision_row(row: sqlite3.Row) -> dict:
    customer = demo.CUSTOMERS.get(row["customer_id"] or "", {})
    return {
        "id": row["id"],
        "time": row["created_at"],
        "agent_id": row["agent_id"],
        "customer_id": row["customer_id"],
        "customer_name": customer.get("name"),
        "tool": row["tool"],
        "amount": _amount(row),
        "decision": row["decision"],
        "rule": row["reason_code"],
        "codes": _json(row["codes"], []),
        "paypal_call": _paypal_call(row),
        "paypal_id": _paypal_id(row),
        "status": _status(row),
        "review_id": row["review_id"],
        "review_status": row["review_status"],
        # An ALLOW that executes a request the owner approved, rather than one the agent made directly.
        "owner_approved": row["decision"] == "ALLOW" and row["review_id"] is not None,
    }


def decisions(db: sqlite3.Connection, limit: int = GRID_LIMIT) -> list[dict]:
    rows = db.execute(_DECISIONS_SQL + " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?", (limit,)).fetchall()
    return [decision_row(row) for row in rows]


def _mandate_view(mandate: Mandate | None) -> dict | None:
    if mandate is None:
        return None
    return {
        "id": mandate.id,
        "version": mandate.version,
        "status": mandate.status,
        "source_text": mandate.source_text,
        "refunds": mandate.refunds.model_dump(mode="json") if mandate.refunds else None,
        "invoices": mandate.invoices.model_dump(mode="json") if mandate.invoices else None,
    }


def decision_detail(
    db: sqlite3.Connection, call_id: str, mandates: Callable[[str], Mandate | None]
) -> dict | None:
    row = db.execute(_DECISIONS_SQL + " WHERE c.id = ?", (call_id,)).fetchone()
    if row is None:
        return None
    summary = decision_row(row)
    ledger = db.execute("SELECT * FROM refund_ledger WHERE call_id = ?", (call_id,)).fetchone()
    review_row = db.execute("SELECT * FROM reviews WHERE id = ?", (row["review_id"],)).fetchone() if row["review_id"] else None
    review = review_view(db, review_row) if review_row else None
    webhooks = []
    if ledger and ledger["refund_id"]:
        webhooks = [
            {"id": w["id"], "event_type": w["event_type"], "received_at": w["received_at"], "result": _json(w["result"])}
            for w in db.execute(
                "SELECT * FROM webhook_events WHERE resource_id IN (?, ?) ORDER BY received_at",
                (ledger["refund_id"], ledger["capture_id"]),
            )
        ]

    timeline = [{"at": row["created_at"], "what": f"{row['agent_id']} called {row['tool']}: {row['decision']}"}]
    if review_row:
        timeline.append({"at": review_row["created_at"], "what": f"held for the owner ({review_row['id']})"})
        if review_row["resolved_at"]:
            note = f": {review_row['note']}" if review_row["note"] else ""
            timeline.append({
                "at": review_row["resolved_at"],
                "what": f"{review_row['status'].lower()} by {review_row['resolved_by']}{note}",
            })
    if row["forwarded"]:
        timeline.append({"at": row["created_at"], "what": f"sent to PayPal ({row['duration_ms'] or '?'} ms): {row['outcome']}"})
    if ledger:
        timeline.append({
            "at": ledger["updated_at"],
            "what": f"ledger {ledger['status']}, PayPal status {ledger['paypal_status'] or 'not reported'}",
        })
    timeline += [{"at": w["received_at"], "what": f"webhook {w['event_type']}"} for w in webhooks]

    return {
        **summary,
        # 1. What the owner authorized: the version in force when this call was decided
        "mandate": _mandate_view(
            (row["mandate_id"] and MandateStore(db).get(row["mandate_id"], row["mandate_version"]))
            or mandates(row["agent_id"] or "")
        ),
        # 2. What the agent tried, exactly as it sent it
        "arguments": _json(row["arguments"], {}),
        # 3. What PayPal says (the facts as checked, and PayPal's own response if the call was sent)
        "paypal_response": _json(row["result"]) if row["forwarded"] else None,
        # 4. Rule by rule
        "checks": _json(row["checks"], []),
        "ledger": dict(ledger) if ledger else None,
        "review": review,
        "webhooks": webhooks,
        "timeline": sorted(timeline, key=lambda item: item["at"] or ""),
    }


def summary(db: sqlite3.Connection, mandates: list[Mandate], now: datetime) -> dict:
    """Today's (UTC) numbers for the KPI row."""
    day = now.date().isoformat()
    rows = db.execute(
        "SELECT decision, review_id, resolved_amount, arguments, outcome, tool FROM tool_calls WHERE substr(created_at, 1, 10) = ?",
        (day,),
    ).fetchall()

    def total(selected) -> str:
        return f"{sum((Decimal(_amount(r) or '0') for r in selected), Decimal('0')):.2f}"

    refunds = [r for r in rows if r["tool"] == "create_refund"]
    auto = [r for r in refunds if r["decision"] == "ALLOW" and r["review_id"] is None and r["outcome"] == "OK"]
    approved = [r for r in refunds if r["decision"] == "ALLOW" and r["review_id"] is not None and r["outcome"] == "OK"]
    held = [r for r in refunds if r["decision"] == "REVIEW"]
    blocked = [r for r in rows if r["decision"] == "BLOCK"]
    blocked_refunds = [r for r in blocked if r["tool"] == "create_refund"]

    caps = []
    for mandate in mandates:
        if mandate.refunds and mandate.refunds.daily_total_max is not None:
            used = Ledger(db).snapshot(mandate, None, now).refunded_today
            caps.append({
                "mandate_id": mandate.id,
                "agent_id": mandate.agent_id,
                "used": f"{used:.2f}",
                "cap": f"{mandate.refunds.daily_total_max:.2f}",
            })

    return {
        "day": day,
        "refunds_auto": {"count": len(auto), "amount": total(auto)},
        "refunds_owner_approved": {"count": len(approved), "amount": total(approved)},
        "held_for_review": {"count": len(held), "amount": total(held)},
        "pending_reviews": db.execute("SELECT COUNT(*) FROM reviews WHERE status = 'PENDING'").fetchone()[0],
        "blocked": {"count": len(blocked)},
        # Money an agent tried to move that the rules stopped.
        "protected": total(blocked_refunds),
        "daily_caps": caps,
        "calls_today": len(rows),
    }


def snapshot(state) -> dict:
    mandates = [m for m in (state.mandates(agent_id) for agent_id in demo.MANDATES) if m is not None]
    return {
        "summary": summary(state.db, mandates, state.clock()),
        "decisions": decisions(state.db),
        "reviews": list_reviews(state, "PENDING"),
    }


def routes(get_state: Callable[[], object | None]) -> list[Route]:
    def not_ready() -> JSONResponse:
        return JSONResponse({"error": "E_NOT_READY", "message": "The gateway is still starting."}, 503)

    async def get_decisions(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        try:
            limit = max(1, min(int(request.query_params.get("limit", GRID_LIMIT)), GRID_LIMIT))
        except ValueError:
            limit = GRID_LIMIT
        return JSONResponse(decisions(state.db, limit))

    async def get_decision(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        detail = decision_detail(state.db, request.path_params["call_id"], state.mandates)
        if detail is None:
            return JSONResponse({"error": "E_CALL_UNKNOWN"}, 404)
        return JSONResponse(detail)

    async def get_summary(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        return JSONResponse(snapshot(state)["summary"])

    async def stream(request: Request) -> StreamingResponse:
        async def events():
            last, idle = None, 0.0
            while not await request.is_disconnected():
                state = get_state()
                if state is not None:
                    body = json.dumps(snapshot(state))
                    if body != last:
                        last, idle = body, 0.0
                        yield f"event: snapshot\ndata: {body}\n\n"
                if idle >= KEEPALIVE_S:
                    idle = 0.0
                    yield ": keepalive\n\n"
                await anyio.sleep(STREAM_TICK_S)
                idle += STREAM_TICK_S

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return [
        Route("/owner/decisions", get_decisions, methods=["GET"]),
        Route("/owner/decisions/{call_id}", get_decision, methods=["GET"]),
        Route("/owner/summary", get_summary, methods=["GET"]),
        Route("/owner/stream", stream, methods=["GET"]),
    ]
