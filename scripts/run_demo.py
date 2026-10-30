"""Start the whole demo locally: build the web apps if needed, then run the gateway and the store.

    python scripts/run_demo.py            # Ctrl+C stops everything
    python scripts/run_demo.py --rebuild  # rebuild the store and console first

Prerequisites (see the README): .env with sandbox credentials and a Gemini key, `npm ci` in
paypal-mcp/, the Python venv, scripts/seed_orders.py (or reset_demo.py) and register_agents.py.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
APPS = [REPO / "apps" / "store", REPO / "apps" / "console"]
SERVICES = [
    ("gateway", [sys.executable, "-m", "intentguard.gateway"], "http://127.0.0.1:8765/health"),
    ("store", [sys.executable, "-m", "intentguard.storefront"], "http://127.0.0.1:8766/api/customers"),
]


def build(app: Path, force: bool) -> None:
    if (app / "dist").exists() and not force:
        return
    npm = shutil.which("npm") or "npm"
    print(f"building {app.relative_to(REPO)} ...")
    if not (app / "node_modules").exists():
        subprocess.run([npm, "ci"], cwd=app, check=True)
    subprocess.run([npm, "run", "build"], cwd=app, check=True)


def preflight() -> list[str]:
    problems = []
    if not (REPO / ".env").exists():
        problems.append("no .env: copy .env.example and fill in the sandbox credentials and GEMINI_API_KEY")
    if not (REPO / "paypal-mcp" / "node_modules").exists():
        problems.append("PayPal's MCP server isn't installed: run `npm ci` in paypal-mcp/")
    if not (REPO / "data" / "seed_orders.json").exists():
        problems.append("no demo orders: run scripts/reset_demo.py")
    if not (REPO / "data" / "agent_keys.json").exists():
        problems.append("no agent keys: run scripts/register_agents.py")
    return problems


def wait_healthy(name: str, url: str, process: subprocess.Popen, timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"{name} exited during startup (code {process.returncode}); see its output above")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit(f"{name} didn't become healthy within {timeout_s:.0f}s")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # show progress even when output goes to a file
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    problems = preflight()
    if problems:
        print("Not ready yet:\n" + "\n".join(f"  - {p}" for p in problems))
        raise SystemExit(1)
    for app in APPS:
        build(app, args.rebuild)

    processes = []
    try:
        for name, command, url in SERVICES:
            print(f"starting {name} ...")
            process = subprocess.Popen(command, cwd=REPO / "backend")
            processes.append(process)
            wait_healthy(name, url, process)
        keys = json.loads((REPO / "data" / "agent_keys.json").read_text(encoding="utf-8"))
        print(
            "\nIntentGuard demo is running:\n"
            "  Store (customer chat):  http://127.0.0.1:8766\n"
            "  Owner console:          http://127.0.0.1:8765/console/\n"
            f"  Owner key:              the \"owner\" entry in data/agent_keys.json{'' if 'owner' in keys else ' (missing: rerun register_agents.py)'}\n"
            "  Checkpoint (no LLM):    python scripts/checkpoint.py --runs 1\n"
            "Ctrl+C to stop.\n"
        )
        while all(p.poll() is None for p in processes):
            time.sleep(1)
        print("a service stopped; shutting down")
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
