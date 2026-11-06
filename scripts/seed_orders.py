"""Create captured sandbox orders for the demo customers and write them to data/seed_orders.json.

    python scripts/seed_orders.py                 # pay with a PayPal sandbox test card (no login needed)
    python scripts/seed_orders.py --approve-links # pay with PayPal wallet: you approve each order in a browser

Card mode needs a sandbox business account that is enabled for card processing (a US business
account is). Each order's purchase unit carries custom_id = the store's customer ID, which is
what the gateway later uses to bind a refund to the customer in the chat.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard import demo_data  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--approve-links", action="store_true", help="pay via PayPal wallet with manual approval")
    orders = demo_data.seed_orders(parser.parse_args().approve_links)
    demo_data.write_orders(orders)
    print(f"\nWrote {len(orders)} orders to {demo_data.SEED_FILE}")
