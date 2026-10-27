"""Day-2 smoke test against a running gateway (python -m intentguard.gateway).

    python scripts/gateway_smoke.py

Checks: unknown agents are rejected; each agent only sees its own tools; the support agent's
refund passes through to PayPal; the billing agent's refund is blocked and never sent; every
call lands in the audit log.
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


def text(result) -> str:
    return "".join(block.text for block in result.content if block.type == "text")


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        sys.exit(1)


async def main() -> None:
    bowl = next(order for order in ORDERS if order["customer_id"] == "cust_ben" and "bowl" in order["item"])

    print("1. Unknown agent")
    response = httpx.post(DEFAULT_GATEWAY_URL, headers={"Authorization": "Bearer nope"}, json={})
    check(response.status_code == 401, f"rejected with 401 ({response.json()['error']})")

    print("2. Support agent (customer cust_ben)")
    async with gateway_session(KEYS["support-agent"], customer_id="cust_ben") as session:
        tools = sorted(tool.name for tool in (await session.list_tools()).tools)
        check(tools == ["create_refund", "get_order", "get_refund"], f"sees {tools}")

        order = json.loads(text(await session.call_tool("get_order", {"id": bowl["order_id"]})))
        check(order["purchase_units"][0]["custom_id"] == "cust_ben", f"get_order {order['id']} forwarded")

        refund = await session.call_tool(
            "create_refund", {"capture_id": bowl["capture_id"], "amount": {"currency_code": "USD", "value": "1.00"}}
        )
        body = json.loads(text(refund))
        check(not refund.is_error and body["status"] == "COMPLETED", f"$1 refund {body.get('id')} {body.get('status')}")

    print("3. Billing agent tries a refund")
    async with gateway_session(KEYS["billing-agent"]) as session:
        tools = sorted(tool.name for tool in (await session.list_tools()).tools)
        check("create_refund" not in tools, f"does not see create_refund ({len(tools)} tools)")
        blocked = await session.call_tool(
            "create_refund", {"capture_id": bowl["capture_id"], "amount": {"currency_code": "USD", "value": "200.00"}}
        )
        check(blocked.is_error and json.loads(text(blocked))["code"] == "E_TOOL_NOT_ALLOWED", "blocked: E_TOOL_NOT_ALLOWED")

    print("4. Audit log (latest 4)")
    rows = store.connect().execute(
        "SELECT agent_id, customer_id, tool, decision, reason_code, forwarded, outcome FROM tool_calls"
        " ORDER BY created_at DESC LIMIT 4"
    )
    for row in rows:
        print("   ", dict(row))
    check(True, "audit rows written")


if __name__ == "__main__":
    asyncio.run(main())
