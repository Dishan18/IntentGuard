"""Day-3 check: evaluate the demo scenarios against real sandbox facts. Read-only; no money moves.

    python scripts/policy_smoke.py
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard.config import REPO_ROOT, get_settings  # noqa: E402
from intentguard.demo import KNOWN_CUSTOMER_EMAILS, MANDATES  # noqa: E402
from intentguard.gateway.facts import FactsService  # noqa: E402
from intentguard.paypal.auth import TokenProvider  # noqa: E402
from intentguard.paypal.rest import PayPalRest  # noqa: E402
from intentguard.policy.engine import evaluate  # noqa: E402
from intentguard.policy.models import AgentView, CaptureFacts, EvaluationInput, LedgerSnapshot  # noqa: E402

ORDERS = json.loads((REPO_ROOT / "data" / "seed_orders.json").read_text(encoding="utf-8"))
SUPPORT = AgentView("support-agent", True, frozenset({"get_order", "get_refund", "create_refund"}))


def order(customer, word):
    return next(o for o in ORDERS if o["customer_id"] == customer and word in o["item"].lower())


def refund(capture_id, value=None):
    args = {"capture_id": capture_id}
    if value:
        args["amount"] = {"currency_code": "USD", "value": value}
    return args


SCENARIOS = [
    ("A  Ana: $20 for broken mug", "cust_ana", "create_refund", refund(order("cust_ana", "mug")["capture_id"], "20.00")),
    ("B  Ben: injected $900 refund", "cust_ben", "create_refund", refund(order("cust_ben", "coffee")["capture_id"], "900.00")),
    ("B  Ben: refund Carla's order", "cust_ben", "create_refund", refund(order("cust_carla", "dinner")["capture_id"], "20.00")),
    ("C  Carla: full $160 dinner set", "cust_carla", "create_refund", refund(order("cust_carla", "dinner")["capture_id"], "160.00")),
    ("-  Dev: refund, no amount given", "cust_dev", "create_refund", refund(order("cust_dev", "planter")["capture_id"])),
    ("-  Ben: look up Dev's order", "cust_ben", "get_order", {"id": order("cust_dev", "planter")["order_id"]}),
    ("-  Ana: made-up capture id", "cust_ana", "create_refund", refund("0ABCDEF1234567890", "10.00")),
]

if __name__ == "__main__":
    settings = get_settings()
    facts_service = FactsService(PayPalRest(settings, TokenProvider(settings)))
    now = datetime.now(UTC)
    for label, customer, tool, arguments in SCENARIOS:
        facts = facts_service.for_call(tool, arguments)
        decision = evaluate(
            EvaluationInput(
                agent=SUPPORT,
                mandate=MANDATES["support-agent"],
                tool=tool,
                arguments=arguments,
                session_customer=customer,
                facts=facts,
                ledger=LedgerSnapshot(),
                known_customer_emails=KNOWN_CUSTOMER_EMAILS,
                now=now,
            )
        )
        refundable = f" (PayPal: ${facts.refundable} refundable)" if isinstance(facts, CaptureFacts) else ""
        amount = f" amount=${decision.resolved_amount}" if decision.resolved_amount is not None else ""
        print(f"{label:<34} {decision.outcome.value:<6} {', '.join(decision.codes) or '-'}{amount}{refundable}")
