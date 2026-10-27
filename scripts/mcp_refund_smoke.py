"""Day-1 smoke test: refund a seeded capture through PayPal's MCP server, then confirm via REST.

    python scripts/mcp_refund_smoke.py             # partial $5 refund of the first seeded order
    python scripts/mcp_refund_smoke.py --amount 10 --capture-id <id>

This calls PayPal MCP directly (no IntentGuard policy yet). It proves the downstream path
the gateway will forward ALLOWed calls to.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard.config import REPO_ROOT, get_settings  # noqa: E402
from intentguard.paypal.auth import TokenProvider  # noqa: E402
from intentguard.paypal.mcp_client import paypal_mcp_session  # noqa: E402
from intentguard.paypal.rest import PayPalRest  # noqa: E402

SEED_FILE = REPO_ROOT / "data" / "seed_orders.json"


async def main(capture_id: str | None, amount: str) -> None:
    if capture_id is None:
        if not SEED_FILE.exists():
            sys.exit(f"{SEED_FILE} not found. Run scripts/seed_orders.py first, or pass --capture-id.")
        capture_id = json.loads(SEED_FILE.read_text(encoding="utf-8"))[0]["capture_id"]

    settings = get_settings()
    tokens = TokenProvider(settings)
    arguments = {"capture_id": capture_id, "amount": {"currency_code": "USD", "value": amount}}
    print(f"MCP create_refund {json.dumps(arguments)}")

    async with paypal_mcp_session(tokens.get(), settings.paypal_environment) as session:
        result = await session.call_tool("create_refund", arguments)

    text = "".join(block.text for block in result.content if block.type == "text")
    if result.is_error:
        sys.exit(f"create_refund failed: {text}")
    refund = json.loads(text)
    print(f"Refund {refund['id']} status {refund['status']}")

    confirmed = PayPalRest(settings, tokens).get_refund(refund["id"])
    print(f"REST confirms: {confirmed['status']} {confirmed['amount']['value']} {confirmed['amount']['currency_code']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capture-id")
    parser.add_argument("--amount", default="5.00")
    args = parser.parse_args()
    asyncio.run(main(args.capture_id, args.amount))
