"""Versioned mandates: what the owner has authorized each agent to do.

Each agent has one mandate ID (`mnd_support`, `mnd_billing`) with numbered versions, so the
ledger's per-mandate counters carry across edits. A version is a DRAFT until the owner activates
it; activating supersedes the previous ACTIVE version in the same transaction. Nothing, including
the Mandate Compiler, can make a draft active except the owner's explicit call.

Every audit row records the mandate ID and version it was decided under, so the console can show
exactly which rules applied, even after they change.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from intentguard.policy.models import Mandate


class MandateError(ValueError):
    pass


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


class MandateStore:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def _load(self, row: sqlite3.Row | None) -> Mandate | None:
        if row is None:
            return None
        return Mandate.model_validate_json(row["policy"]).model_copy(
            update={"version": row["version"], "status": "ACTIVE" if row["status"] == "ACTIVE" else "PAUSED"}
        )

    def active(self, agent_id: str) -> Mandate | None:
        """The mandate the gateway enforces for this agent right now."""
        return self._load(
            self._db.execute("SELECT * FROM mandates WHERE agent_id = ? AND status = 'ACTIVE'", (agent_id,)).fetchone()
        )

    def get(self, mandate_id: str, version: int) -> Mandate | None:
        return self._load(
            self._db.execute("SELECT * FROM mandates WHERE id = ? AND version = ?", (mandate_id, version)).fetchone()
        )

    def row(self, mandate_id: str, version: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM mandates WHERE id = ? AND version = ?", (mandate_id, version)).fetchone()

    def history(self, agent_id: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM mandates WHERE agent_id = ? ORDER BY version DESC", (agent_id,)
        ).fetchall()

    def seed(self, mandates: Iterable[Mandate], now: datetime) -> None:
        """Install the built-in demo mandates as v1 for agents that don't have one yet."""
        for mandate in mandates:
            exists = self._db.execute("SELECT 1 FROM mandates WHERE agent_id = ?", (mandate.agent_id,)).fetchone()
            if exists:
                continue
            self._db.execute(
                "INSERT INTO mandates (id, version, agent_id, status, policy, source_text, compiled_by, created_at,"
                " activated_at, activated_by) VALUES (?, 1, ?, 'ACTIVE', ?, ?, 'seed', ?, ?, 'seed')",
                (mandate.id, mandate.agent_id, mandate.model_copy(update={"version": 1}).model_dump_json(),
                 mandate.source_text, _iso(now), _iso(now)),
            )

    def create_draft(self, mandate: Mandate, compiled_by: str, notes: dict, now: datetime) -> int:
        """Store a compiled mandate as the next version, inactive. Returns the version number."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            version = self._db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM mandates WHERE id = ?", (mandate.id,)
            ).fetchone()[0]
            self._db.execute(
                "INSERT INTO mandates (id, version, agent_id, status, policy, source_text, compiled_by, notes, created_at)"
                " VALUES (?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?)",
                (mandate.id, version, mandate.agent_id, mandate.model_copy(update={"version": version}).model_dump_json(),
                 mandate.source_text, compiled_by, json.dumps(notes), _iso(now)),
            )
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return version

    def activate(self, mandate_id: str, version: int, owner: str, now: datetime) -> Mandate:
        """Make a draft the enforced mandate. Only the owner API calls this."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self.row(mandate_id, version)
            if row is None:
                raise MandateError(f"no mandate {mandate_id} v{version}")
            if row["status"] != "DRAFT":
                raise MandateError(f"{mandate_id} v{version} is {row['status']}, not a draft")
            blocking = (json.loads(row["notes"] or "{}")).get("blocking") or []
            if blocking:
                raise MandateError("this draft has problems that must be fixed first: " + "; ".join(blocking))
            self._db.execute(
                "UPDATE mandates SET status = 'SUPERSEDED' WHERE agent_id = ? AND status = 'ACTIVE'", (row["agent_id"],)
            )
            self._db.execute(
                "UPDATE mandates SET status = 'ACTIVE', activated_at = ?, activated_by = ? WHERE id = ? AND version = ?",
                (_iso(now), owner, mandate_id, version),
            )
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return self.active(row["agent_id"])
