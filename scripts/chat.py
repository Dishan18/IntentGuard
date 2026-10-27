"""Chat with the Support Agent as a demo customer. The gateway must be running.

    python scripts/chat.py --customer cust_ana                       # interactive
    python scripts/chat.py --customer cust_ben --say "..." --say "..."  # scripted turns

The agent talks to PayPal only through IntentGuard; each tool call and its decision is printed.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard.agents.llm import GeminiClient  # noqa: E402
from intentguard.agents.support import Customer, SupportAgent, ToolEvent  # noqa: E402
from intentguard.config import REPO_ROOT  # noqa: E402
from intentguard.demo import CUSTOMERS  # noqa: E402

KEYS_FILE = REPO_ROOT / "data" / "agent_keys.json"
ORDERS_FILE = REPO_ROOT / "data" / "seed_orders.json"


def describe(event: ToolEvent) -> str:
    amount = (event.arguments.get("amount") or {}).get("value")
    what = f"{event.tool}" + (f" ${amount}" if amount else "")
    if event.decision == "BLOCK":
        return f"{what} -> BLOCK {', '.join(event.result.get('codes') or [event.code])}  (PayPal call NOT SENT)"
    if event.decision == "REVIEW":
        return f"{what} -> REVIEW {event.result.get('review_id')}  (waiting for the owner)"
    if event.decision == "ALLOW" and isinstance(event.result, dict) and event.tool == "create_refund":
        return f"{what} -> ALLOW  PayPal refund {event.result.get('id')} {event.result.get('status')}"
    return f"{what} -> {event.decision}"


async def main(customer_id: str, scripted: list[str], compromised: bool) -> None:
    customer = Customer(customer_id, CUSTOMERS[customer_id]["name"])
    orders = [o for o in json.loads(ORDERS_FILE.read_text(encoding="utf-8")) if o["customer_id"] == customer_id]
    key = json.loads(KEYS_FILE.read_text(encoding="utf-8"))["support-agent"]
    agent = SupportAgent(GeminiClient.from_env(), key, compromised=compromised)
    history: list[tuple[str, str]] = []

    mode = " Agent is SIMULATED COMPROMISED (obeys the customer)." if compromised else ""
    print(f"Chatting as {customer.name} ({customer.id}). {len(orders)} orders on file.{mode} Ctrl+C to quit.\n")
    messages = iter(scripted) if scripted else None
    while True:
        if messages is not None:
            message = next(messages, None)
            if message is None:
                return
            print(f"{customer.name}: {message}")
        else:
            message = input(f"{customer.name}: ").strip()
            if not message:
                continue
        reply = await agent.reply(customer, orders, history, message)
        for event in reply.events:
            print(f"   [IntentGuard] {describe(event)}")
        source = f"{reply.model}, LLM unavailable: reply built from IntentGuard's result" if reply.degraded else reply.model
        print(f"Agent ({source}): {reply.text}\n")
        history += [("user", message), ("model", reply.text)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customer", required=True, choices=sorted(CUSTOMERS))
    parser.add_argument("--say", action="append", default=[], help="scripted customer message (repeatable)")
    parser.add_argument("--compromised", action="store_true", help="simulate a jailbroken agent that obeys the customer")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.customer, args.say, args.compromised))
    except (KeyboardInterrupt, EOFError):
        pass
