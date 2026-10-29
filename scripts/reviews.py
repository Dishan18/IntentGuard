"""The owner's review queue from a terminal. The gateway must be running.

    python scripts/reviews.py                          # pending reviews
    python scripts/reviews.py --all                    # every review
    python scripts/reviews.py approve rv_123 --note "Wrong colour, fair enough"
    python scripts/reviews.py reject rv_123 --note "Please return the set first"

Uses the "owner" key from data/agent_keys.json (scripts/register_agents.py creates it).
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard.config import REPO_ROOT  # noqa: E402

KEYS_FILE = REPO_ROOT / "data" / "agent_keys.json"
GATEWAY = "http://127.0.0.1:8765"


def show(review: dict) -> None:
    arguments = review["arguments"]
    print(f"{review['review_id']}  {review['status']:<9} {review['tool']} ${review['amount'] or '?'}"
          f"  customer {review['customer_id']}  capture {arguments.get('capture_id', '-')}  by {review['agent_id']}")
    for reason in review["reasons"]:
        print(f"    needs approval: {reason['code']}  {reason['detail']}")
    if review["status"] != "PENDING":
        print(f"    {review['status'].lower()} by {review['resolved_by']} at {review['resolved_at']}"
              + (f": {review['note']}" if review["note"] else ""))
    if review["execution"]:
        execution = review["execution"]
        print(f"    execution {execution['call_id']}: {execution['decision']} {', '.join(execution['codes'])}"
              f" -> {execution['outcome']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=["approve", "reject"])
    parser.add_argument("review_id", nargs="?")
    parser.add_argument("--note", help="reason (shown to the customer)")
    parser.add_argument("--all", action="store_true", help="list every review, not just pending ones")
    args = parser.parse_args()
    if args.action and not args.review_id:
        parser.error(f"{args.action} needs a review ID")

    key = json.loads(KEYS_FILE.read_text(encoding="utf-8"))["owner"]
    with httpx.Client(base_url=GATEWAY, headers={"Authorization": f"Bearer {key}"}, timeout=90) as http:
        if not args.action:
            reviews = http.get("/owner/reviews", params={} if args.all else {"status": "PENDING"}).json()
            if not reviews:
                print("No reviews." if args.all else "Nothing waiting for approval.")
            for review in reviews:
                show(review)
            return 0
        response = http.post(f"/owner/reviews/{args.review_id}/{args.action}", json={"note": args.note})
        result = response.json()
        print(json.dumps(result, indent=2))
        return 0 if response.status_code == 200 and result.get("status") in ("APPROVED", "REJECTED") else 1


if __name__ == "__main__":
    sys.exit(main())
