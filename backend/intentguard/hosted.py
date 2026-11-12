"""The hosted demo: the gateway and the store in one process on one port (Render's $PORT).

    python -m intentguard.hosted

/mcp, /owner, /console, /webhooks and /health go to the gateway; everything else to the store. The
store's Support Agent still reaches the gateway over HTTP with its agent key, on this same port, so
the request path is the same as when they run as two servers.

On start-up it registers the demo agents, gives the owner the key in INTENTGUARD_OWNER_KEY (so judges
get one stable key) and, if INTENTGUARD_SUPPORT_AGENT_KEY is set, a fixed Support Agent key for
calling /mcp directly, and seeds fresh sandbox orders if there are none: a free Render instance starts
with an empty disk every time it wakes. Expensive endpoints are rate-limited (intentguard.ratelimit).
"""

import logging
import os

from starlette.types import ASGIApp, Receive, Scope, Send

from intentguard.ratelimit import RateLimit

log = logging.getLogger("intentguard.hosted")

GATEWAY_PATHS = ("/mcp", "/owner", "/console", "/webhooks", "/health")


def is_gateway_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in GATEWAY_PATHS)


class Router:
    """Sends each request to the gateway or the store by path. Only the gateway has start-up work
    (PayPal's MCP server, the reconciler), so lifespan events go to it."""

    def __init__(self, gateway: ASGIApp, store_app: ASGIApp):
        self._gateway = gateway
        self._store = store_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or is_gateway_path(scope.get("path", "")):
            await self._gateway(scope, receive, send)
        else:
            await self._store(scope, receive, send)


def bootstrap() -> None:
    from intentguard import demo_data, store

    owner_key = os.getenv("INTENTGUARD_OWNER_KEY", "").strip()
    if not owner_key:
        raise SystemExit("Set INTENTGUARD_OWNER_KEY: it's the key judges use to open the owner console.")
    # Optional: a fixed Support Agent key lets judges call /mcp themselves (the Postman collection),
    # as the store's agent runtime would. Otherwise it's random on every start.
    support_key = os.getenv("INTENTGUARD_SUPPORT_AGENT_KEY", "").strip()
    db = store.connect()
    demo_data.register_agents(db, owner_key, {"support-agent": support_key} if support_key else None)
    log.info("registered the demo agents and the owner key%s", " (fixed Support Agent key)" if support_key else "")
    if demo_data.SEED_FILE.exists():
        return
    try:
        orders = demo_data.reset(say=log.info, db=db)
        log.info("seeded %d sandbox orders", len(orders))
    except Exception:
        # Serve anyway: the owner console's Reset demo button seeds again once PayPal answers.
        log.exception("couldn't seed the demo orders")


def build() -> ASGIApp:
    from intentguard.gateway.app import build_app as build_gateway
    from intentguard.storefront.app import build_app as build_store

    return RateLimit(Router(build_gateway(host="0.0.0.0"), build_store()))


def main() -> None:
    port = int(os.getenv("PORT", "10000"))
    # Before anything imports the gateway client, whose default URL comes from here; hence the
    # imports inside bootstrap() and build().
    os.environ.setdefault("INTENTGUARD_GATEWAY_URL", f"http://127.0.0.1:{port}/mcp")
    # The hosted demo shows judges its owner key on the console's sign-in page (gateway/app.py).
    os.environ.setdefault("INTENTGUARD_SHOW_OWNER_KEY", "1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bootstrap()

    import uvicorn

    uvicorn.run(build(), host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
