"""PayPal webhook receiver: POST /webhooks/paypal.

Every delivery is verified with PayPal (`verify-webhook-signature`) against our webhook ID before
anything else, and refused if that isn't configured or the check fails. Event IDs are recorded, so
a redelivered event is acknowledged without being processed twice. The event itself is only a
pointer: `Reconciler.on_event` reads the refund's real status from PayPal's API.

A delivery is recorded only after it has been processed. If processing fails (PayPal unreachable,
say), we answer 500 and PayPal retries later.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from intentguard.paypal.rest import PayPalAPIError

log = logging.getLogger("intentguard.webhooks")

MAX_BODY_BYTES = 256 * 1024


def already_processed(db: sqlite3.Connection, event_id: str) -> bool:
    return db.execute("SELECT 1 FROM webhook_events WHERE id = ?", (event_id,)).fetchone() is not None


def record(db: sqlite3.Connection, event: dict, result: dict, now: datetime) -> None:
    db.execute(
        "INSERT OR IGNORE INTO webhook_events (id, event_type, resource_id, received_at, result) VALUES (?, ?, ?, ?, ?)",
        (
            event["id"],
            event.get("event_type", ""),
            (event.get("resource") or {}).get("id"),
            now.isoformat(timespec="milliseconds"),
            json.dumps(result),
        ),
    )


def routes(get_state: Callable[[], object | None]) -> list[Route]:
    async def receive(request: Request) -> JSONResponse:
        state = get_state()
        if state is None or state.reconciler is None:
            return JSONResponse({"error": "not ready"}, 503)
        if not state.webhook_id:
            # Fail closed: without our webhook ID nothing can be verified, so nothing is trusted.
            return JSONResponse({"error": "webhooks are not configured (PAYPAL_WEBHOOK_ID)"}, 503)

        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "too large"}, 413)
        try:
            event = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, 400)
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            return JSONResponse({"error": "not a PayPal event"}, 400)

        headers = {name.lower(): value for name, value in request.headers.items()}
        try:
            verified = await anyio.to_thread.run_sync(
                state.reconciler.verify, state.webhook_id, headers, event
            )
        except PayPalAPIError as error:
            log.warning("webhook %s: verification call failed: %s", event["id"], error)
            return JSONResponse({"error": "verification unavailable"}, 503)
        if not verified:
            log.warning("webhook %s (%s): signature verification FAILED", event["id"], event.get("event_type"))
            return JSONResponse({"error": "signature verification failed"}, 400)

        if already_processed(state.db, event["id"]):
            return JSONResponse({"status": "duplicate", "event_id": event["id"]})
        try:
            result = await anyio.to_thread.run_sync(state.reconciler.on_event, event)
        except PayPalAPIError as error:
            log.warning("webhook %s: reconciliation failed, PayPal will retry: %s", event["id"], error)
            return JSONResponse({"error": "processing failed"}, 500)
        record(state.db, event, result, state.clock())
        log.info("webhook %s %s -> %s", event["id"], event.get("event_type"), result)
        return JSONResponse({"status": "processed", "event_id": event["id"], "result": result})

    return [Route("/webhooks/paypal", receive, methods=["POST"])]
