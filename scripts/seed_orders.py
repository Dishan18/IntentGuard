"""Create captured sandbox orders for the demo customers and write them to data/seed_orders.json.

    python scripts/seed_orders.py                 # pay with a PayPal sandbox test card (no login needed)
    python scripts/seed_orders.py --approve-links # pay with PayPal wallet: you approve each order in a browser

Card mode needs a sandbox business account that is enabled for card processing (a US business
account is). Each order's purchase unit carries custom_id = the store's customer ID, which is
what the gateway later uses to bind a refund to the customer in the chat.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard.config import REPO_ROOT, get_settings  # noqa: E402
from intentguard.paypal.auth import TokenProvider  # noqa: E402
from intentguard.paypal.rest import PayPalRest  # noqa: E402

SEED_FILE = REPO_ROOT / "data" / "seed_orders.json"

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


def seed(approve_links: bool) -> list[dict]:
    paypal = PayPalRest(get_settings(), TokenProvider(get_settings()))
    seeded = []
    for customer_id, name, item, amount in DEMO_ORDERS:
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
        seeded.append(
            {
                "customer_id": customer_id,
                "customer_name": name,
                "item": item,
                "amount": amount,
                "currency": "USD",
                "order_id": order["id"],
                "capture_id": capture["id"],
                "capture_status": capture["status"],
            }
        )
        print(f"{customer_id:<11} {item:<34} ${amount:>7}  capture {capture['id']} {capture['status']}")
    return seeded


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approve-links", action="store_true", help="pay via PayPal wallet with manual approval")
    orders = seed(parser.parse_args().approve_links)
    SEED_FILE.parent.mkdir(exist_ok=True)
    SEED_FILE.write_text(json.dumps(orders, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {len(orders)} orders to {SEED_FILE}")
