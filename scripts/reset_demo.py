"""Reset the demo: fresh captured sandbox orders, and an empty ledger, review queue and audit log.

    python scripts/reset_demo.py

Registered agents and their keys are kept. Old sandbox orders stay in PayPal but are no longer used.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_orders  # noqa: E402

from intentguard import store  # noqa: E402

if __name__ == "__main__":
    db = store.connect()
    for table in ("refund_ledger", "reviews", "tool_calls", "webhook_events"):
        deleted = db.execute(f"DELETE FROM {table}").rowcount
        print(f"cleared {table}: {deleted} rows")
    print("seeding fresh orders...")
    orders = seed_orders.seed(approve_links=False)
    seed_orders.SEED_FILE.write_text(seed_orders.json.dumps(orders, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(orders)} orders to {seed_orders.SEED_FILE}")
