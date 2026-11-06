"""Setting up the demo's data: captured sandbox orders, the demo agents and owner key, and a reset.

Used by the scripts (seed_orders.py, register_agents.py, reset_demo.py), by the hosted deployment
on start-up, and by the owner console's Reset demo button.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from intentguard import demo, store
from intentguard.config import REPO_ROOT, get_settings
from intentguard.gateway import registry
from intentguard.gateway.mandates import MandateStore
from intentguard.paypal.auth import TokenProvider
from intentguard.paypal.rest import PayPalRest

SEED_FILE = REPO_ROOT / "data" / "seed_orders.json"
KEYS_FILE = REPO_ROOT / "data" / "agent_keys.json"

# PayPal's published sandbox test card; any future expiry and CVV are accepted.
TEST_CARD = {"number": "4111111111111111", "expiry": "2030-12", "security_code": "123"}

# (customer_id, customer name, item, USD amount)
DEMO_ORDERS = [
    ("cust_ana", "Ana Lima", "Ceramic mug (broken in transit)", "38.00"),
    ("cust_ana", "Ana Lima", "Linen tea towel set", "24.00"),
    ("cust_ben", "Ben Okafor", "Pour-over coffee kit", "42.00"),
    ("cust_ben", "Ben Okafor", "Stoneware bowl", "19.00"),
    ("cust_carla", "Carla Diaz", "Hand-thrown dinner set", "160.00"),
    ("cust_carla", "Carla Diaz", "Serving platter", "65.00"),
    ("cust_dev", "Dev Patel", "Espresso cups (set of 4)", "48.00"),
    ("cust_dev", "Dev Patel", "Large planter", "240.00"),
]

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

OWNER_NAME = "Maya (owner)"

# Cleared by a reset. Agents, owner keys and (unless asked) mandate history are kept.
RESET_TABLES = ("refund_ledger", "reviews", "tool_calls", "webhook_events", "redteam_results", "redteam_runs")


def order_body(customer_id: str, item: str, amount: str) -> dict:
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": customer_id,
                "description": item,
                "amount": {"currency_code": "USD", "value": amount},
            }
        ],
    }


def first_capture(order: dict) -> dict:
    return order["purchase_units"][0]["payments"]["captures"][0]


def seed_orders(approve_links: bool = False, say=print) -> list[dict]:
    """Create a captured sandbox order per demo purchase. Each carries custom_id = the customer,
    which the gateway later uses to bind a refund to the customer in the chat."""
    tokens = TokenProvider(get_settings())
    tokens.get()  # one access token, fetched before the threads that share it
    paypal = PayPalRest(get_settings(), tokens)

    def one(purchase: tuple[str, str, str, str]) -> dict:
        customer_id, name, item, amount = purchase
        body = order_body(customer_id, item, amount)
        if approve_links:
            body["payment_source"] = {"paypal": {"experience_context": {"user_action": "PAY_NOW"}}}
            order = paypal.create_order(body)
            approve = next(link["href"] for link in order["links"] if link["rel"] == "payer-action")
            input(f"\nApprove {item} (${amount}) as a sandbox buyer:\n  {approve}\nPress Enter when done... ")
            order = paypal.capture_order(order["id"])
        else:
            body["payment_source"] = {"card": {**TEST_CARD, "name": name}}
            order = paypal.create_order(body)
            if order["status"] != "COMPLETED":
                order = paypal.capture_order(order["id"])
        capture = first_capture(order)
        return {
            "customer_id": customer_id,
            "customer_name": name,
            "item": item,
            "amount": amount,
            "currency": "USD",
            "order_id": order["id"],
            "capture_id": capture["id"],
            "capture_status": capture["status"],
        }

    if approve_links:  # one browser approval at a time
        seeded = [one(purchase) for purchase in DEMO_ORDERS]
    else:
        # Card orders are independent and take ~4s each; in parallel a hosted cold start seeds in seconds.
        with ThreadPoolExecutor(max_workers=len(DEMO_ORDERS)) as pool:
            seeded = list(pool.map(one, DEMO_ORDERS))
    for o in seeded:
        say(f"{o['customer_id']:<11} {o['item']:<34} ${o['amount']:>7}  capture {o['capture_id']} {o['capture_status']}")
    return seeded


def write_orders(orders: list[dict]) -> None:
    SEED_FILE.parent.mkdir(exist_ok=True)
    SEED_FILE.write_text(json.dumps(orders, indent=2) + "\n", encoding="utf-8")


def register_agents(db: sqlite3.Connection, owner_key: str | None = None) -> dict[str, str]:
    """Register (re-key) the demo agents and the owner, and write the keys to data/agent_keys.json.
    With `owner_key`, the owner gets that key (the hosted demo's, from its environment) instead of a new one."""
    keys = {agent["agent_id"]: registry.register(db, **agent) for agent in DEMO_AGENTS}
    keys["owner"] = registry.register_owner(db, OWNER_NAME, owner_key)
    KEYS_FILE.parent.mkdir(exist_ok=True)
    KEYS_FILE.write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    return keys


def reset(keep_mandates: bool = False, say=print, db: sqlite3.Connection | None = None) -> list[dict]:
    """Fresh captured sandbox orders, built-in mandates, and an empty ledger, review queue, audit log,
    webhook log and red-team history. Old sandbox orders stay in PayPal but are no longer used."""
    db = db or store.connect()
    db.execute("BEGIN IMMEDIATE")
    for table in RESET_TABLES:
        say(f"cleared {table}: {db.execute(f'DELETE FROM {table}').rowcount} rows")
    if not keep_mandates:
        say(f"cleared mandates: {db.execute('DELETE FROM mandates').rowcount} versions")
    db.execute("COMMIT")
    MandateStore(db).seed(demo.MANDATES.values(), datetime.now(UTC))
    say("seeding fresh orders...")
    orders = seed_orders(say=say)
    write_orders(orders)
    say(f"Wrote {len(orders)} orders to {SEED_FILE}")
    return orders
