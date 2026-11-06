"""Reset the demo: fresh captured sandbox orders, built-in mandates, and an empty ledger, review
queue, audit log, webhook log and red-team history.

    python scripts/reset_demo.py
    python scripts/reset_demo.py --keep-mandates   # keep the mandate versions the owner made

Registered agents and their keys are kept. Old sandbox orders stay in PayPal but are no longer used.
Safe while the gateway is running: the mandates are reseeded in the same step. The owner console's
Reset demo button does the same thing.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard import demo_data  # noqa: E402


def reset(keep_mandates: bool = False, quiet: bool = False) -> list[dict]:
    return demo_data.reset(keep_mandates, say=(lambda *a: None) if quiet else print)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-mandates", action="store_true")
    reset(parser.parse_args().keep_mandates)
