"""Owner API for the demo itself (under /owner, owner key only).

    POST /owner/demo/reset   fresh sandbox orders, built-in mandates, and an empty ledger, review
                             queue, audit log and red-team history (the console's Reset demo button)
"""

import logging
from collections.abc import Callable

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from intentguard import demo_data
from intentguard.gateway.owner import OWNER_SCOPE_KEY

log = logging.getLogger("intentguard.demo")


def routes(get_state: Callable[[], object | None], reset: Callable[..., list[dict]] = demo_data.reset) -> list[Route]:
    lock = anyio.Lock()

    async def post_reset(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return JSONResponse({"error": "E_NOT_READY", "message": "The gateway is still starting."}, 503)
        if lock.locked():
            return JSONResponse({"error": "E_RESET_IN_PROGRESS", "message": "A reset is already running."}, 409)
        if state.db.execute("SELECT 1 FROM redteam_runs WHERE status = 'RUNNING'").fetchone():
            return JSONResponse(
                {"error": "E_RUN_IN_PROGRESS", "message": "Wait for the red-team run to finish, then reset."}, 409
            )
        async with lock:
            log.info("demo reset by %s", request.scope.get(OWNER_SCOPE_KEY, "the owner"))
            try:
                # Its own connection, in a worker thread: seeding makes ~16 PayPal calls.
                orders = await anyio.to_thread.run_sync(lambda: reset(say=log.info))
            except Exception as error:
                log.exception("demo reset failed")
                return JSONResponse(
                    {"error": "E_RESET_FAILED", "message": f"Couldn't reseed the demo ({type(error).__name__})."}, 502
                )
            state.shadows.clear()
        return JSONResponse({"orders": len(orders), "message": f"Demo reset: {len(orders)} fresh sandbox orders."})

    return [Route("/owner/demo/reset", post_reset, methods=["POST"])]
