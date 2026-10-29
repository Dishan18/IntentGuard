"""Keeps the refund ledger in line with PayPal, which is the source of truth for what happened.

Two triggers, one code path:

- **Webhooks** (`gateway/webhooks.py`): a verified PayPal event names a refund or capture, and that
  entry is reconciled straight away. The event's payload is only a pointer: the status is read from
  PayPal's API, not taken from the payload.
- **Poller**: every minute, any entry whose outcome isn't final is checked. This also covers a
  missed or late webhook, and runs without a public URL.

What gets reconciled:

- `UNKNOWN` (the gateway couldn't tell whether PayPal executed a refund). Every forwarded refund
  carries its call ID as PayPal's `invoice_id`, so the reconciler looks for that refund on the
  capture. Found means COMMITTED with PayPal's status. Not found after a grace period means the
  request never took effect, and the reservation is released.
- `COMMITTED` refunds that PayPal reported as PENDING (or without a status), until they settle.
  FAILED or CANCELLED means no money moved, so the entry is released and stops counting.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

import anyio

from intentguard.gateway.facts import FactsService
from intentguard.gateway.ledger import Ledger
from intentguard.paypal.rest import PayPalAPIError, PayPalRest

log = logging.getLogger("intentguard.reconcile")

# How long an UNKNOWN entry waits for its refund to show up at PayPal before we conclude it never
# took effect. PayPal creates refunds synchronously, so this is generous.
UNKNOWN_GRACE = timedelta(minutes=10)

REFUND_EVENTS = ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.REFUND.PENDING", "PAYMENT.REFUND.FAILED")


def _link(resource: dict, rel: str) -> str | None:
    return next((link["href"] for link in resource.get("links", []) if link.get("rel") == rel), None)


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Reconciler:
    def __init__(
        self, db: sqlite3.Connection, paypal: PayPalRest, facts: FactsService, clock: Callable[[], datetime]
    ):
        self._db = db
        self._paypal = paypal
        self._facts = facts
        self._clock = clock

    @property
    def ledger(self) -> Ledger:
        return Ledger(self._db)

    def verify(self, webhook_id: str, headers: dict[str, str], event: dict) -> bool:
        return self._paypal.verify_webhook_signature(webhook_id, headers, event)

    # ------------------------------------------------------------ entries

    def reconcile_entry(self, entry: sqlite3.Row) -> dict | None:
        """Check one ledger entry against PayPal. Returns what changed, or None.
        Reads from PayPal happen outside the write lock; the update is one short transaction."""
        if entry["status"] == "UNKNOWN" or not entry["refund_id"]:
            refunds = self._facts.refunds_for_capture(entry["capture_id"])
            match = next((r for r in refunds if self._invoice_id(r) == entry["call_id"]), None)
        else:
            match = self._paypal.get_refund(entry["refund_id"])

        now = self._clock()
        with self.ledger.transaction():
            if match is not None:
                status = self.ledger.apply_paypal_status(entry["call_id"], match["id"], match.get("status", ""), now)
                change = status and {"refund_id": match["id"], "paypal_status": match.get("status"), "ledger": status}
            elif entry["status"] == "UNKNOWN" and _when(entry["created_at"]) < now - UNKNOWN_GRACE:
                change = self.ledger.release_unknown(entry["call_id"], now) and {"paypal_status": "NOT_FOUND", "ledger": "RELEASED"}
            else:
                change = None
        if change:
            change = {"call_id": entry["call_id"], **change}
            log.info("reconciled %s", change)
        return change or None

    def _invoice_id(self, refund: dict) -> str | None:
        # The order's refund list may omit fields; the refund itself always has them.
        if "invoice_id" in refund:
            return refund["invoice_id"]
        return self._paypal.get_refund(refund["id"]).get("invoice_id")

    async def poll(self, interval_s: float) -> None:
        """The poller: a pass at startup (to pick up anything left UNKNOWN), then every interval."""
        while True:
            try:
                changes = await anyio.to_thread.run_sync(self.run_once)
                if changes:
                    log.info("poller reconciled %d entries", len(changes))
            except Exception:
                log.exception("poller pass failed")
            await anyio.sleep(interval_s)

    def run_once(self) -> list[dict]:
        """One poller pass over every entry whose outcome isn't final."""
        changes = []
        for entry in self.ledger.needing_reconciliation():
            try:
                change = self.reconcile_entry(entry)
            except PayPalAPIError as error:
                log.warning("couldn't reconcile %s: %s", entry["call_id"], error)
                continue
            if change:
                changes.append(change)
        return changes

    # ------------------------------------------------------------ webhook events

    def on_event(self, event: dict) -> dict:
        """Reconcile whatever a verified PayPal event points at. Returns a summary for the event log."""
        event_type = event.get("event_type", "")
        resource = event.get("resource") or {}
        if event_type not in REFUND_EVENTS:
            return {"action": "ignored"}

        up = _link(resource, "up") or ""
        if "/captures/" in up:  # the resource is a refund
            refund_id = resource.get("id")
            capture_id = up.rstrip("/").rsplit("/", 1)[-1]
        else:  # the resource is the capture itself
            refund_id, capture_id = None, resource.get("id")

        entries = []
        if refund_id and (entry := self.ledger.by_refund_id(refund_id)) is not None:
            entries = [entry]
        elif capture_id:
            # Not linked yet (an UNKNOWN entry, say): look at everything we hold for this capture.
            entries = self._db.execute(
                "SELECT * FROM refund_ledger WHERE capture_id = ? AND status IN ('UNKNOWN', 'COMMITTED')", (capture_id,)
            ).fetchall()
        changes = [change for entry in entries if (change := self.reconcile_entry(entry))]
        if not entries:
            # A refund made outside IntentGuard (in the PayPal dashboard, say). Policy facts are read
            # live from PayPal, so the next decision on this capture already accounts for it.
            return {"action": "external", "refund_id": refund_id, "capture_id": capture_id}
        return {"action": "reconciled", "refund_id": refund_id, "capture_id": capture_id, "changes": changes}
