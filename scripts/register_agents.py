"""Register the demo agents and the store owner, and write their keys to data/agent_keys.json.

    python scripts/register_agents.py

Re-running re-keys everything (old keys stop working). The "owner" key is for the owner's review
API (scripts/reviews.py, the console); it is not an agent key and can't call PayPal tools.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard import store  # noqa: E402
from intentguard.config import REPO_ROOT  # noqa: E402
from intentguard.gateway import registry  # noqa: E402

KEYS_FILE = REPO_ROOT / "data" / "agent_keys.json"

DEMO_AGENTS = [
    {
        "agent_id": "support-agent",
        "name": "Support Agent",
        "owner": "Maya's Pottery (demo merchant)",
        "purpose": "Answers customer chat; looks up orders and issues refunds.",
        "allowed_tools": ["get_order", "get_refund", "create_refund"],
    },
    {
        "agent_id": "billing-agent",
        "name": "Billing Agent",
        "owner": "Maya's Pottery (demo merchant)",
        "purpose": "Drafts and sends invoices and payment reminders.",
        "allowed_tools": [
            "create_invoice",
            "get_invoice",
            "list_invoices",
            "send_invoice",
            "send_invoice_reminder",
            "cancel_sent_invoice",
        ],
    },
]

if __name__ == "__main__":
    db = store.connect()
    keys = {agent["agent_id"]: registry.register(db, **agent) for agent in DEMO_AGENTS}
    keys["owner"] = registry.register_owner(db, "Maya (owner)")
    KEYS_FILE.parent.mkdir(exist_ok=True)
    KEYS_FILE.write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    for agent in DEMO_AGENTS:
        print(f"{agent['agent_id']:<14} tools: {', '.join(agent['allowed_tools'])}")
    print("owner          review API (approve/reject held requests)")
    print(f"Keys written to {KEYS_FILE}")
