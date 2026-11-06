"""Register the demo agents and the store owner, and write their keys to data/agent_keys.json.

    python scripts/register_agents.py

Re-running re-keys everything (old keys stop working). The "owner" key is for the owner's review
API (scripts/reviews.py, the console); it is not an agent key and can't call PayPal tools.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from intentguard import demo_data, store  # noqa: E402

if __name__ == "__main__":
    demo_data.register_agents(store.connect())
    for agent in demo_data.DEMO_AGENTS:
        print(f"{agent['agent_id']:<14} tools: {', '.join(agent['allowed_tools'])}")
    print("owner          review API (approve/reject held requests)")
    print(f"Keys written to {demo_data.KEYS_FILE}")
