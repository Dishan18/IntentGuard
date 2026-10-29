"""Run one reconciliation pass now (the gateway's poller does this every minute on its own).

    python scripts/reconcile.py

Prints every ledger entry whose outcome isn't final, then what PayPal says about each.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard import store  # noqa: E402
from intentguard.config import get_settings  # noqa: E402
from intentguard.gateway.facts import FactsService  # noqa: E402
from intentguard.gateway.reconcile import Reconciler  # noqa: E402
from intentguard.paypal.auth import TokenProvider  # noqa: E402
from intentguard.paypal.rest import PayPalRest  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    paypal = PayPalRest(settings, TokenProvider(settings))
    db = store.connect()
    reconciler = Reconciler(db, paypal, FactsService(paypal), lambda: datetime.now(UTC))
    pending = reconciler.ledger.needing_reconciliation()
    for entry in pending:
        print(f"open  {entry['call_id']}  {entry['status']:<9} ${entry['amount']}  capture {entry['capture_id']}"
              f"  refund {entry['refund_id'] or '-'}  PayPal {entry['paypal_status'] or '-'}")
    changes = reconciler.run_once()
    for change in changes:
        print("fixed", change)
    print(f"{len(pending)} open, {len(changes)} reconciled")
