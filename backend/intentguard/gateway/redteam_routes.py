"""Owner API for the red team (under /owner, owner key only).

    POST /owner/redteam/runs           {compromised: true, generated: 0-5} → starts a dry-run attack run
    GET  /owner/redteam/runs/latest    the latest run's report (results arrive while it runs)
    GET  /owner/redteam/runs/{run_id}
"""

import os
from collections.abc import Callable

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from intentguard import redteam
from intentguard.agents.llm import LLMUnavailable
from intentguard.gateway import registry
from intentguard.gateway.client import DEFAULT_GATEWAY_URL
from intentguard.gateway.server import DRY_RUN_AGENTS

REDTEAM_AGENT = "redteam-agent"
MAX_GENERATED = 5


def routes(get_state: Callable[[], object | None]) -> list[Route]:
    gateway_url = os.getenv("INTENTGUARD_GATEWAY_URL", DEFAULT_GATEWAY_URL)

    def not_ready() -> JSONResponse:
        return JSONResponse({"error": "E_NOT_READY", "message": "The gateway is still starting."}, 503)

    async def start(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        if state.compiler_llm is None:
            return JSONResponse({"error": "E_LLM_UNAVAILABLE", "message": "Set GEMINI_API_KEY to run the red team."}, 503)
        if state.db.execute("SELECT 1 FROM redteam_runs WHERE status = 'RUNNING'").fetchone():
            return JSONResponse({"error": "E_RUN_IN_PROGRESS", "message": "A red-team run is already in progress."}, 409)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        compromised = body.get("compromised", True) is not False
        generated = max(0, min(int(body.get("generated") or 0), MAX_GENERATED))

        orders = redteam.load_orders()
        if not orders:
            return JSONResponse({"error": "E_NO_ORDERS", "message": "Seed the demo first (scripts/reset_demo.py)."}, 409)
        target = registry.get(state.db, DRY_RUN_AGENTS[REDTEAM_AGENT])
        if target is None:
            return JSONResponse({"error": "E_AGENT_UNKNOWN", "message": "Register the demo agents first."}, 409)
        # A fresh key per run, held only in memory: this identity can't reach PayPal anyway.
        key = registry.register(
            state.db, REDTEAM_AGENT, "Red team", "IntentGuard",
            "Plays attacks against the Support Agent's rules in dry-run mode; can never reach PayPal.",
            sorted(target.allowed_tools),
        )
        attacks = redteam.catalog(orders)
        run_id = redteam.start_run(state.db, compromised, len(attacks) + generated)

        async def go() -> None:
            extra = []
            if generated:
                try:
                    extra = await redteam.generate(state.compiler_llm, orders, generated)
                except (LLMUnavailable, ValueError) as error:
                    redteam.log.warning("couldn't generate attacks: %s", error)
            await redteam.run(
                state.db, state.compiler_llm, key, run_id, attacks + extra, orders, compromised,
                forget_scenario=lambda scenario: state.shadows.pop(scenario, None), gateway_url=gateway_url,
            )

        return JSONResponse({"run_id": run_id, "total": len(attacks) + generated}, 202, background=BackgroundTask(go))

    async def latest(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        return JSONResponse(redteam.report(state.db) or {"run_id": None})

    async def one(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        found = redteam.report(state.db, request.path_params["run_id"])
        return JSONResponse(found or {"error": "E_RUN_UNKNOWN"}, 200 if found else 404)

    return [
        Route("/owner/redteam/runs", start, methods=["POST"]),
        Route("/owner/redteam/runs/latest", latest, methods=["GET"]),
        Route("/owner/redteam/runs/{run_id}", one, methods=["GET"]),
    ]
