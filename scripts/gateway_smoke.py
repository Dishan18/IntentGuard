"""End-to-end check of the demo scenarios through a running gateway (python -m intentguard.gateway).

    python scripts/reset_demo.py      # fresh orders, empty ledger
    python scripts/gateway_smoke.py

Moves real sandbox money: one $20 refund (scenario A). Everything else must be stopped before PayPal.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard import store  # noqa: E402
from intentguard.config import REPO_ROOT  # noqa: E402
from intentguard.gateway.client import DEFAULT_GATEWAY_URL, gateway_session  # noqa: E402

KEYS = json.loads((REPO_ROOT / "data" / "agent_keys.json").read_text(encoding="utf-8"))
ORDERS = json.loads((REPO_ROOT / "data" / "seed_orders.json").read_text(encoding="utf-8"))


def order(customer, word):
    return next(o for o in ORDERS if o["customer_id"] == customer and word in o["item"].lower())


def refund(customer, word, value=None):
    arguments = {"capture_id": order(customer, word)["capture_id"]}
    if value:
        arguments["amount"] = {"currency_code": "USD", "value": value}
    return arguments


def text(result) -> str:
    return "".join(block.text for block in result.content if block.type == "text")


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        sys.exit(1)


async def call(key, customer, tool, arguments):
    async with gateway_session(key, customer_id=customer) as session:
        result = await session.call_tool(tool, arguments)
    return result, json.loads(text(result))


async def main() -> None:
    support, billing = KEYS["support-agent"], KEYS["billing-agent"]

    print("0. Unknown agent")
    response = httpx.post(DEFAULT_GATEWAY_URL, headers={"Authorization": "Bearer nope"}, json={})
    check(response.status_code == 401, "rejected with 401 E_AGENT_UNKNOWN")

    print("A. Ana: $20 for the broken mug")
    result, body = await call(support, "cust_ana", "create_refund", refund("cust_ana", "mug", "20.00"))
    check(not result.is_error and body["status"] == "COMPLETED", f"ALLOW -> PayPal refund {body.get('id')} {body.get('status')}")
    result, body = await call(support, "cust_ana", "create_refund", refund("cust_ana", "towel", "5.00"))
    check(body.get("code") == "E_FREQUENCY", "second refund in 30 days: BLOCK E_FREQUENCY")

    print("B. Ben: prompt-injected requests")
    result, body = await call(support, "cust_ben", "create_refund", refund("cust_ben", "coffee", "900.00"))
    check(result.is_error and body["paypal_call"] == "NOT_SENT", f"$900: BLOCK {', '.join(body['codes'])}, NOT_SENT")
    print(f"        agent is told: \"{body['message']}\"")
    result, body = await call(support, "cust_ben", "create_refund", refund("cust_carla", "dinner", "20.00"))
    check(body.get("code") == "E_SUBJECT_MISMATCH", "refund Carla's order: BLOCK E_SUBJECT_MISMATCH")
    result, body = await call(support, "cust_ben", "get_order", {"id": order("cust_dev", "planter")["order_id"]})
    check(body.get("code") == "E_SUBJECT_MISMATCH", "read Dev's order: BLOCK E_SUBJECT_MISMATCH")

    print("C. Carla: $160 dinner set")
    result, body = await call(support, "cust_carla", "create_refund", refund("cust_carla", "dinner", "160.00"))
    check(body.get("decision") == "REVIEW", f"REVIEW {body.get('review_id')}, PayPal {body.get('paypal_call')}")

    print("D. Billing agent tries a refund")
    result, body = await call(billing, None, "create_refund", refund("cust_dev", "planter", "200.00"))
    check(body.get("code") == "E_TOOL_NOT_ALLOWED", "BLOCK E_TOOL_NOT_ALLOWED")

    print("Ledger and audit")
    db = store.connect()
    ledger = [dict(row) for row in db.execute("SELECT customer_id, amount, status, refund_id FROM refund_ledger")]
    check(len(ledger) == 1 and ledger[0]["status"] == "COMMITTED", f"ledger: {ledger}")
    forwarded = db.execute("SELECT COUNT(*) FROM tool_calls WHERE forwarded = 1").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    check(forwarded == 1, f"{total} calls audited, {forwarded} forwarded to PayPal")


if __name__ == "__main__":
    asyncio.run(main())
