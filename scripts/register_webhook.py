"""Register (or list / delete) the PayPal sandbox webhook that points at the gateway.

    python scripts/register_webhook.py --url https://<public-host>/webhooks/paypal
    python scripts/register_webhook.py --list
    python scripts/register_webhook.py --delete WH-...

PayPal needs a public HTTPS URL: the deployed gateway, or a tunnel to a local one. Put the printed
webhook ID in .env as PAYPAL_WEBHOOK_ID and restart the gateway; until then it refuses webhooks.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard.config import get_settings  # noqa: E402
from intentguard.gateway.reconcile import REFUND_EVENTS  # noqa: E402
from intentguard.paypal.auth import TokenProvider  # noqa: E402
from intentguard.paypal.rest import PayPalRest  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="public HTTPS URL ending in /webhooks/paypal")
    group.add_argument("--list", action="store_true")
    group.add_argument("--delete", metavar="WEBHOOK_ID")
    args = parser.parse_args()

    settings = get_settings()
    paypal = PayPalRest(settings, TokenProvider(settings))
    if args.list:
        for hook in paypal.list_webhooks():
            print(hook["id"], hook["url"], ", ".join(e["name"] for e in hook.get("event_types", [])))
    elif args.delete:
        paypal.delete_webhook(args.delete)
        print(f"Deleted {args.delete}")
    else:
        if not args.url.startswith("https://") or not args.url.endswith("/webhooks/paypal"):
            parser.error("--url must be https://.../webhooks/paypal")
        hook = paypal.create_webhook(args.url, list(REFUND_EVENTS))
        print(f"Created webhook {hook['id']} -> {hook['url']}")
        print(f"Add to .env:  PAYPAL_WEBHOOK_ID={hook['id']}  (then restart the gateway)")
