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
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    agent_id     TEXT,
    customer_id  TEXT,
    tool         TEXT NOT NULL,
    arguments    TEXT NOT NULL,           -- JSON
    decision     TEXT NOT NULL,           -- FORWARD | BLOCK
    reason_code  TEXT,
    forwarded    INTEGER NOT NULL DEFAULT 0,
    outcome      TEXT NOT NULL,           -- PENDING | OK | PAYPAL_ERROR | OUTCOME_UNKNOWN | NOT_SENT
    result       TEXT,                    -- PayPal's response text, or our error
    duration_ms  INTEGER
);
"""


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection
