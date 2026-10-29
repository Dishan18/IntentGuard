"""The gateway's HTTP app: MCP for agents at /mcp, the owner's review API at /owner, PayPal
webhooks at /webhooks/paypal (verified by signature, not by key), and /health."""

import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from intentguard import store
from intentguard.gateway import owner, registry, webhooks
from intentguard.gateway.server import AGENT_SCOPE_KEY, GatewayState, build_server


def _bearer(scope: Scope) -> str:
    return Request(scope).headers.get("authorization", "").removeprefix("Bearer ").strip()


class AuthMiddleware:
    """/mcp needs a valid, active agent key; /owner needs an owner key. Agent keys never work on
    /owner and owner keys never work on /mcp."""

    def __init__(self, app: ASGIApp, db: sqlite3.Connection):
        self._app = app
        self._db = db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            key = _bearer(scope)
            agent = registry.authenticate(self._db, key) if key else None
            if agent is None or not agent.active:
                response = JSONResponse({"error": "E_AGENT_UNKNOWN", "message": "Missing or invalid agent key."}, 401)
                await response(scope, receive, send)
                return
            scope[AGENT_SCOPE_KEY] = agent
        elif scope["type"] == "http" and scope["path"].startswith("/owner"):
            key = _bearer(scope)
            name = registry.authenticate_owner(self._db, key) if key else None
            if name is None:
                response = JSONResponse({"error": "E_OWNER_UNKNOWN", "message": "Missing or invalid owner key."}, 401)
                await response(scope, receive, send)
                return
            scope[owner.OWNER_SCOPE_KEY] = name
        await self._app(scope, receive, send)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app(db: sqlite3.Connection | None = None, host: str = "127.0.0.1") -> ASGIApp:
    db = db or store.connect()
    running: list[GatewayState | None] = [None]

    def on_state(state: GatewayState | None) -> None:
        running[0] = state

    server = build_server(db, on_state)
    app = server.streamable_http_app(
        host=host,
        custom_starlette_routes=[
            Route("/health", health),
            *owner.routes(lambda: running[0]),
            *webhooks.routes(lambda: running[0]),
        ],
    )
    return AuthMiddleware(app, db)
