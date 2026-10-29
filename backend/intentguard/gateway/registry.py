"""Know-Your-Agent registry: who each agent is, who owns it, and which PayPal tools it may use.

Agents authenticate to the gateway with a bearer API key. Only the key's sha256 is stored.
"""

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    owner: str
    purpose: str
    allowed_tools: frozenset[str]
    status: str

    @property
    def active(self) -> bool:
        return self.status == "ACTIVE"


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def register(
    db: sqlite3.Connection, agent_id: str, name: str, owner: str, purpose: str, allowed_tools: list[str]
) -> str:
    """Create or re-key an agent. Returns the new API key; it can't be recovered later."""
    api_key = f"ig_{agent_id}_{secrets.token_urlsafe(24)}"
    db.execute(
        """
        INSERT INTO agents (id, name, owner, purpose, allowed_tools, key_hash) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name, owner = excluded.owner, purpose = excluded.purpose,
            allowed_tools = excluded.allowed_tools, key_hash = excluded.key_hash, status = 'ACTIVE'
        """,
        (agent_id, name, owner, purpose, json.dumps(sorted(allowed_tools)), hash_key(api_key)),
    )
    return api_key


def authenticate(db: sqlite3.Connection, api_key: str) -> Agent | None:
    return _agent(db.execute("SELECT * FROM agents WHERE key_hash = ?", (hash_key(api_key),)).fetchone())


def get(db: sqlite3.Connection, agent_id: str) -> Agent | None:
    return _agent(db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone())


def _agent(row: sqlite3.Row | None) -> Agent | None:
    if row is None:
        return None
    return Agent(
        id=row["id"],
        name=row["name"],
        owner=row["owner"],
        purpose=row["purpose"],
        allowed_tools=frozenset(json.loads(row["allowed_tools"])),
        status=row["status"],
    )


def register_owner(db: sqlite3.Connection, name: str) -> str:
    """Create (or replace) a key for the store owner's API (review decisions). Returns the key once."""
    owner_key = f"igo_{secrets.token_urlsafe(24)}"
    db.execute("DELETE FROM owner_keys WHERE name = ?", (name,))
    db.execute("INSERT INTO owner_keys (key_hash, name) VALUES (?, ?)", (hash_key(owner_key), name))
    return owner_key


def authenticate_owner(db: sqlite3.Connection, owner_key: str) -> str | None:
    row = db.execute("SELECT name FROM owner_keys WHERE key_hash = ?", (hash_key(owner_key),)).fetchone()
    return row["name"] if row else None
