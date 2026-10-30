"""Checkpoint: scenarios A–D end to end through a running gateway and the real PayPal sandbox,
N times in a row, each from a fresh reset. No LLM: the calls an agent would make are made directly,
so a failure means IntentGuard or PayPal, not the model.

    python -m intentguard.gateway                 # in another terminal
    python scripts/checkpoint.py                  # 3 runs
    python scripts/checkpoint.py --runs 1 --no-reset

Moves sandbox money and sends sandbox invoices: per run, a $38 and a $160 refund and a $48 invoice.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reset_demo import reset  # noqa: E402

from intentguard import store  # noqa: E402
from intentguard.config import REPO_ROOT, get_settings  # noqa: E402
from intentguard.gateway.client import DEFAULT_GATEWAY_URL, gateway_session  # noqa: E402
from intentguard.paypal.auth import TokenProvider  # noqa: E402
from intentguard.paypal.rest import PayPalRest  # noqa: E402

GATEWAY = DEFAULT_GATEWAY_URL.removesuffix("/mcp")
KEYS = json.loads((REPO_ROOT / "data" / "agent_keys.json").read_text(encoding="utf-8"))


class CheckFailed(Exception):
    pass


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise CheckFailed(label)


def body_of(result) -> dict:
    return json.loads("".join(block.text for block in result.content if block.type == "text"))


async def call(key: str, customer: str | None, tool: str, arguments: dict) -> tuple[bool, dict]:
    async with gateway_session(key, customer_id=customer) as session:
        result = await session.call_tool(tool, arguments)
    return result.is_error, body_of(result)


async def run_once(orders: list[dict], paypal: PayPalRest) -> None:
    def capture(customer: str, word: str) -> str:
        return next(o["capture_id"] for o in orders if o["customer_id"] == customer and word in o["item"].lower())

    def order_id(customer: str, word: str) -> str:
        return next(o["order_id"] for o in orders if o["customer_id"] == customer and word in o["item"].lower())

    support, billing, owner = KEYS["support-agent"], KEYS["billing-agent"], KEYS["owner"]
    owner_http = httpx.Client(base_url=GATEWAY, headers={"Authorization": f"Bearer {owner}"}, timeout=120)

    print("0. Identity")
    check(httpx.post(DEFAULT_GATEWAY_URL, headers={"Authorization": "Bearer nope"}, json={}).status_code == 401,
          "unknown agent key: 401 E_AGENT_UNKNOWN")
    as_agent = httpx.get(f"{GATEWAY}/owner/reviews", headers={"Authorization": f"Bearer {support}"})
    check(as_agent.status_code == 401, "agent key on the owner API: 401")

    print("A. Ana: full refund of the broken $38 mug (no amount given)")
    error, body = await call(support, "cust_ana", "create_refund", {"capture_id": capture("cust_ana", "mug")})
    check(not error and body.get("status") == "COMPLETED", f"ALLOW, PayPal refund {body.get('id')} COMPLETED")
    refund = paypal.get_refund(body["id"])
    check(refund["amount"]["value"] == "38.00", "PayPal refunded exactly the resolved $38.00")
    check(str(refund.get("invoice_id", "")).startswith("call_"), f"refund carries IntentGuard's call ID ({refund.get('invoice_id')})")
    error, body = await call(support, "cust_ana", "create_refund",
                             {"capture_id": capture("cust_ana", "towel"), "amount": {"currency_code": "USD", "value": "5.00"}})
    check(body.get("code") == "E_FREQUENCY", "second refund in 30 days: BLOCK E_FREQUENCY")

    print("B. Ben: what a prompt-injected agent tries")
    error, body = await call(support, "cust_ben", "create_refund",
                             {"capture_id": capture("cust_ben", "coffee"), "amount": {"currency_code": "USD", "value": "900.00"}})
    check(error and body["paypal_call"] == "NOT_SENT" and "E_EXCEEDS_CAPTURE" in body["codes"],
          f"$900: BLOCK {', '.join(body['codes'])}, NOT_SENT")
    error, body = await call(support, "cust_ben", "create_refund", {"capture_id": capture("cust_carla", "dinner")})
    check(body.get("code") == "E_SUBJECT_MISMATCH", "refund Carla's order from Ben's chat: BLOCK E_SUBJECT_MISMATCH")
    error, body = await call(support, "cust_ben", "get_order", {"id": order_id("cust_dev", "planter")})
    check(body.get("code") == "E_SUBJECT_MISMATCH", "read Dev's order from Ben's chat: BLOCK E_SUBJECT_MISMATCH")

    print("C. Carla: $160 needs Maya")
    error, body = await call(support, "cust_carla", "create_refund",
                             {"capture_id": capture("cust_carla", "dinner"), "amount": {"currency_code": "USD", "value": "160.00"}})
    check(not error and body.get("decision") == "REVIEW" and body["paypal_call"] == "NOT_SENT_YET",
          f"REVIEW {body.get('review_id')}, PayPal not called yet")
    review_id = body["review_id"]
    approved = owner_http.post(f"/owner/reviews/{review_id}/approve", json={"note": "checkpoint"}).json()
    check(approved.get("status") == "APPROVED" and approved["paypal"]["status"] == "COMPLETED",
          f"owner approves: re-checked, PayPal refund {approved.get('paypal', {}).get('id')} COMPLETED")
    check(owner_http.post(f"/owner/reviews/{review_id}/approve").status_code == 409, "approving twice: 409, not refunded twice")

    print("D. Billing Agent: its own identity and rules")
    error, body = await call(billing, None, "create_refund",
                             {"capture_id": capture("cust_dev", "planter"), "amount": {"currency_code": "USD", "value": "200.00"}})
    check(body.get("code") == "E_TOOL_NOT_ALLOWED", "goodwill refund: BLOCK E_TOOL_NOT_ALLOWED")

    def invoice(email: str, name: str, value: str) -> dict:
        given, surname = name.split(" ", 1)
        return {
            "currency_code": "USD",
            "invoicer_business_name": "Maya's Pottery",
            "primary_recipients": [{"billing_info": {"email_address": email, "name": {"given_name": given, "surname": surname}}}],
            "items": [{"name": "Espresso cups (set of 4)", "quantity": "1", "unit_amount": {"currency_code": "USD", "value": value}}],
        }

    error, body = await call(billing, None, "create_invoice", invoice("jordan@unknown-buyer.net", "Jordan Lee", "300.00"))
    check(body.get("code") == "E_UNKNOWN_RECIPIENT", "invoice to a non-customer: BLOCK E_UNKNOWN_RECIPIENT")
    error, body = await call(billing, None, "create_invoice", invoice("dev.patel@example.com", "Dev Patel", "48.00"))
    invoice_id = str(body.get("href", "")).rstrip("/").rsplit("/", 1)[-1]
    check(not error and invoice_id.startswith("INV2-"), f"create a $48 invoice for Dev: ALLOW ({invoice_id})")
    error, body = await call(billing, None, "send_invoice", {"invoice_id": invoice_id})
    check(not error and body.get("ok") is not False, "send it: ALLOW")
    check(paypal.get_invoice(invoice_id)["status"] == "SENT", "PayPal says the invoice is SENT")

    print("Ledger, audit and console")
    db = store.connect()
    ledger = [dict(r) for r in db.execute("SELECT amount, status, paypal_status FROM refund_ledger ORDER BY created_at")]
    check([(e["amount"], e["status"], e["paypal_status"]) for e in ledger]
          == [("38.00", "COMMITTED", "COMPLETED"), ("160.00", "COMMITTED", "COMPLETED")], f"ledger: {ledger}")
    leaked = db.execute("SELECT COUNT(*) FROM tool_calls WHERE decision != 'ALLOW' AND forwarded = 1").fetchone()[0]
    forwarded = db.execute("SELECT COUNT(*) FROM tool_calls WHERE forwarded = 1").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    check(leaked == 0 and forwarded == 4, f"{total} calls audited, {forwarded} sent to PayPal, 0 blocked calls sent")
    unpinned = db.execute("SELECT COUNT(*) FROM tool_calls WHERE mandate_version IS NULL").fetchone()[0]
    check(unpinned == 0, "every decision records its mandate version")
    summary = owner_http.get("/owner/summary").json()
    check(summary["protected"] == "1265.00" and summary["daily_caps"][0]["used"] == "198.00",
          f"console: ${summary['protected']} protected, ${summary['daily_caps'][0]['used']} of today's cap used")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--no-reset", action="store_true", help="use the current orders and ledger (one run only)")
    args = parser.parse_args()
    if httpx.get(f"{GATEWAY}/health").status_code != 200:
        print("The gateway isn't running: python -m intentguard.gateway")
        return 2

    settings = get_settings()
    paypal = PayPalRest(settings, TokenProvider(settings))
    passed = 0
    for run in range(1, args.runs + 1):
        print(f"\n=== Run {run} of {args.runs}")
        started = time.monotonic()
        if args.no_reset:
            orders = json.loads((REPO_ROOT / "data" / "seed_orders.json").read_text(encoding="utf-8"))
        else:
            print("resetting the demo (fresh sandbox orders)...")
            orders = reset(quiet=True)
        try:
            asyncio.run(run_once(orders, paypal))
        except CheckFailed:
            print(f"Run {run} FAILED")
            break
        passed += 1
        print(f"Run {run} passed in {time.monotonic() - started:.0f}s")
    print(f"\n{passed} of {args.runs} runs passed")
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
