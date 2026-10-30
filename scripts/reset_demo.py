"""Reset the demo: fresh captured sandbox orders, built-in mandates, and an empty ledger, review
queue, audit log and webhook log.

    python scripts/reset_demo.py
    python scripts/reset_demo.py --keep-mandates   # keep the mandate versions the owner made

Registered agents and their keys are kept. Old sandbox orders stay in PayPal but are no longer used.
Safe while the gateway is running: the mandates are reseeded in the same step.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_orders  # noqa: E402

from intentguard import demo, store  # noqa: E402
from intentguard.gateway.mandates import MandateStore  # noqa: E402

TABLES = ("refund_ledger", "reviews", "tool_calls", "webhook_events")


def reset(keep_mandates: bool = False, quiet: bool = False) -> list[dict]:
    say = (lambda *a: None) if quiet else print
    db = store.connect()
    db.execute("BEGIN IMMEDIATE")
    for table in TABLES:
        say(f"cleared {table}: {db.execute(f'DELETE FROM {table}').rowcount} rows")
    if not keep_mandates:
        say(f"cleared mandates: {db.execute('DELETE FROM mandates').rowcount} versions")
    db.execute("COMMIT")
    MandateStore(db).seed(demo.MANDATES.values(), datetime.now(UTC))
    say("seeding fresh orders...")
    orders = seed_orders.seed(approve_links=False)
    seed_orders.SEED_FILE.write_text(json.dumps(orders, indent=2) + "\n", encoding="utf-8")
    say(f"Wrote {len(orders)} orders to {seed_orders.SEED_FILE}")
    return orders


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-mandates", action="store_true")
    reset(parser.parse_args().keep_mandates)
