"""Refund ledger with atomic reservations, plus the review queue.

The gateway evaluates a refund and reserves its amount inside one `BEGIN IMMEDIATE` transaction.
SQLite allows only one such writer at a time, so concurrent requests are evaluated one after
another against an up-to-date snapshot, and the daily total can't be overspent by a race.

Amounts are stored as decimal strings and summed with Decimal, never as floats.
"""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal

from intentguard.policy.models import LedgerSnapshot, Mandate

# Statuses that count as money spent. UNKNOWN counts until reconciled: if we can't tell whether
# PayPal executed a refund, assume it did.
SPENT = ("RESERVED", "COMMITTED", "UNKNOWN")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


class Ledger:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def snapshot(self, mandate: Mandate, customer_id: str | None, now: datetime) -> LedgerSnapshot:
        policy = mandate.refunds
        window_start = _iso(now - timedelta(days=policy.per_customer_window_days if policy else 30))
        placeholders = ", ".join("?" * len(SPENT))

        customer_refunds = 0
        if customer_id:
            customer_refunds = self._db.execute(
                f"SELECT COUNT(*) FROM refund_ledger WHERE mandate_id = ? AND customer_id = ?"
                f" AND created_at >= ? AND status IN ({placeholders})",
                (mandate.id, customer_id, window_start, *SPENT),
            ).fetchone()[0]
            # A refund waiting for the owner counts too, so asking again while one is pending
            # can't slip a second refund through.
            customer_refunds += self._db.execute(
                "SELECT COUNT(*) FROM reviews WHERE mandate_id = ? AND customer_id = ? AND tool = 'create_refund'"
                " AND status = 'PENDING'",
                (mandate.id, customer_id),
            ).fetchone()[0]

        amounts = self._db.execute(
            f"SELECT amount FROM refund_ledger WHERE mandate_id = ? AND day = ? AND status IN ({placeholders})",
            (mandate.id, now.date().isoformat(), *SPENT),
        )
        refunded_today = sum((Decimal(row["amount"]) for row in amounts), Decimal("0"))
        return LedgerSnapshot(customer_refunds_in_window=customer_refunds, refunded_today=refunded_today)

    def reserve(
        self,
        call_id: str,
        mandate: Mandate,
        agent_id: str,
        customer_id: str | None,
        capture_id: str,
        amount: Decimal,
        now: datetime,
    ) -> None:
        self._db.execute(
            "INSERT INTO refund_ledger"
            " (call_id, mandate_id, agent_id, customer_id, capture_id, amount, status, day, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?)",
            (call_id, mandate.id, agent_id, customer_id, capture_id, str(amount), now.date().isoformat(), _iso(now), _iso(now)),
        )

    def _settle(self, call_id: str, status: str, now: datetime, refund_id: str | None = None) -> None:
        self._db.execute(
            "UPDATE refund_ledger SET status = ?, refund_id = COALESCE(?, refund_id), updated_at = ?"
            " WHERE call_id = ? AND status = 'RESERVED'",
            (status, refund_id, _iso(now), call_id),
        )

    def commit(self, call_id: str, refund_id: str | None, now: datetime) -> None:
        self._settle(call_id, "COMMITTED", now, refund_id)

    def release(self, call_id: str, now: datetime) -> None:
        self._settle(call_id, "RELEASED", now)

    def mark_unknown(self, call_id: str, now: datetime) -> None:
        self._settle(call_id, "UNKNOWN", now)


class Reviews:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def create(
        self,
        call_id: str,
        mandate: Mandate,
        agent_id: str,
        customer_id: str | None,
        tool: str,
        arguments: dict,
        amount: Decimal | None,
        codes: list[str],
        now: datetime,
    ) -> str:
        review_id = f"rv_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT INTO reviews (id, call_id, mandate_id, agent_id, customer_id, tool, arguments, amount, codes,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
            (
                review_id,
                call_id,
                mandate.id,
                agent_id,
                customer_id,
                tool,
                json.dumps(arguments),
                None if amount is None else str(amount),
                json.dumps(codes),
                _iso(now),
            ),
        )
        return review_id
