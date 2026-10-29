"""SQLite storage. One file under data/, created on first use, so judges need no database setup."""

import sqlite3
from pathlib import Path

from intentguard.config import REPO_ROOT

DEFAULT_DB_PATH = REPO_ROOT / "data" / "intentguard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    owner         TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    allowed_tools TEXT NOT NULL,          -- JSON array of PayPal MCP tool names
    key_hash      TEXT NOT NULL UNIQUE,   -- sha256 of the agent's API key; the key itself is never stored
    status        TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One row per tool call an agent makes. The row is written before anything is
-- forwarded to PayPal, so a crash mid-call leaves an OUTCOME_UNKNOWN row to reconcile
-- rather than no trace at all.
CREATE TABLE IF NOT EXISTS tool_calls (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    agent_id        TEXT,
    customer_id     TEXT,
    tool            TEXT NOT NULL,
    arguments       TEXT NOT NULL,        -- JSON, as the agent sent them
    decision        TEXT NOT NULL,        -- ALLOW | REVIEW | BLOCK
    reason_code     TEXT,                 -- first blocking (or review) code
    codes           TEXT,                 -- JSON array of every failing/review code
    checks          TEXT,                 -- JSON array of {rule, result, detail, code}
    resolved_amount TEXT,                 -- the amount that would move, as a decimal string
    review_id       TEXT,
    forwarded       INTEGER NOT NULL DEFAULT 0,
    outcome         TEXT NOT NULL,        -- PENDING | OK | PAYPAL_ERROR | OUTCOME_UNKNOWN | NOT_SENT | AWAITING_REVIEW
    result          TEXT,                 -- PayPal's response text, or our error
    duration_ms     INTEGER
);

-- Money IntentGuard has let an agent move (or is about to). Amounts are decimal strings.
-- RESERVED rows are written in the same transaction as the ALLOW decision, so two concurrent
-- requests can't both see the same remaining budget.
CREATE TABLE IF NOT EXISTS refund_ledger (
    call_id     TEXT PRIMARY KEY,
    mandate_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    customer_id TEXT,
    capture_id  TEXT NOT NULL,
    amount      TEXT NOT NULL,
    status      TEXT NOT NULL,            -- RESERVED | COMMITTED | RELEASED | UNKNOWN
    refund_id   TEXT,
    day         TEXT NOT NULL,            -- UTC date the reservation counts against
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refund_ledger_customer ON refund_ledger (mandate_id, customer_id, created_at);
CREATE INDEX IF NOT EXISTS refund_ledger_day ON refund_ledger (mandate_id, day);

-- Calls the policy sent to the owner. The stored arguments are what will be executed on approval.
CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL,
    mandate_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    customer_id TEXT,
    tool        TEXT NOT NULL,
    arguments   TEXT NOT NULL,            -- JSON, with the resolved amount filled in
    amount      TEXT,
    codes       TEXT NOT NULL,            -- JSON
    status      TEXT NOT NULL,            -- PENDING | APPROVED | REJECTED | EXPIRED | FAILED
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS reviews_pending ON reviews (status, mandate_id, customer_id);

-- Keys for the store owner's API (review queue). Separate from agent keys, so an agent key can
-- never approve its own requests.
CREATE TABLE IF NOT EXISTS owner_keys (
    key_hash   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

# Columns added after the first schema; applied to existing databases on connect.
ADDED_COLUMNS = {
    "tool_calls": {
        "codes": "TEXT",
        "checks": "TEXT",
        "resolved_amount": "TEXT",
        "review_id": "TEXT",
    },
    "reviews": {
        "execution_call_id": "TEXT",  # the tool_calls row that executed the approved request
    },
}


def _add_missing_columns(connection: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    _add_missing_columns(connection)
    return connection
