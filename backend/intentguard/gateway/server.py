"""IntentGuard MCP gateway.

Agents connect here (streamable HTTP, `/mcp`) instead of to PayPal. Each agent authenticates
with its API key and only sees, and can only call, the PayPal tools in its allowlist. Every call
is written to the audit log before it is forwarded.

The runtime that hosts an agent (not the LLM) sends `X-IntentGuard-Customer` with the
authenticated customer the conversation belongs to; the policy engine binds actions to it.
"""

import json
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from intentguard import store
from intentguard.config import get_settings
from intentguard.gateway import registry
from intentguard.gateway.downstream import DownstreamUnavailable, PayPalDownstream
from intentguard.paypal.auth import TokenProvider

CUSTOMER_HEADER = "x-intentguard-customer"
AGENT_SCOPE_KEY = "intentguard.agent"


@dataclass
class GatewayState:
    db: sqlite3.Connection
    downstream: PayPalDownstream


def _agent(ctx: ServerRequestContext) -> registry.Agent:
    # Set by AgentAuthMiddleware; requests without a valid key never reach the handlers.
    return ctx.request.scope[AGENT_SCOPE_KEY]


def _customer(ctx: ServerRequestContext) -> str | None:
    return ctx.request.headers.get(CUSTOMER_HEADER)


def _error_result(code: str, message: str) -> types.CallToolResult:
    body = {"decision": "BLOCK", "code": code, "message": message}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))], is_error=True)


def _result_text(result: types.CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, types.TextContent))


async def list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    state: GatewayState = ctx.lifespan_context
    agent = _agent(ctx)
    tools = [tool for name, tool in sorted(state.downstream.tools.items()) if name in agent.allowed_tools]
    return types.ListToolsResult(tools=tools)


async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    state: GatewayState = ctx.lifespan_context
    agent = _agent(ctx)
    customer_id = _customer(ctx)
    arguments = params.arguments or {}
    call_id = f"call_{uuid.uuid4().hex[:16]}"

    def record(**fields) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        state.db.execute(f"UPDATE tool_calls SET {columns} WHERE id = ?", (*fields.values(), call_id))

    allowed = params.name in agent.allowed_tools and params.name in state.downstream.tools
    state.db.execute(
        "INSERT INTO tool_calls (id, agent_id, customer_id, tool, arguments, decision, reason_code, outcome)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_id,
            agent.id,
            customer_id,
            params.name,
            json.dumps(arguments),
            "FORWARD" if allowed else "BLOCK",
            None if allowed else "E_TOOL_NOT_ALLOWED",
            "PENDING" if allowed else "NOT_SENT",
        ),
    )
    if not allowed:
        return _error_result("E_TOOL_NOT_ALLOWED", f"Agent {agent.id} is not permitted to call {params.name}.")

    started = time.monotonic()
    try:
        result = await state.downstream.call_tool(params.name, arguments)
    except DownstreamUnavailable as error:
        record(outcome="NOT_SENT", result=str(error))
        return _error_result("E_DOWNSTREAM_UNAVAILABLE", "PayPal is unreachable; nothing was sent.")
    except Exception as error:
        # The request may have reached PayPal. Never retry blindly; reconcile instead.
        record(forwarded=1, outcome="OUTCOME_UNKNOWN", result=repr(error))
        raise

    record(
        forwarded=1,
        outcome="PAYPAL_ERROR" if result.is_error else "OK",
        result=_result_text(result),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


def build_server(db: sqlite3.Connection) -> Server[GatewayState]:
    @asynccontextmanager
    async def lifespan(_: Server) -> AsyncIterator[GatewayState]:
        settings = get_settings()
        downstream = PayPalDownstream(settings, TokenProvider(settings))
        async with anyio.create_task_group() as task_group:
            await task_group.start(downstream.run)
            yield GatewayState(db=db, downstream=downstream)
            task_group.cancel_scope.cancel()

    return Server(
        "intentguard",
        version="0.1.0",
        instructions="IntentGuard policy gateway in front of PayPal. Blocked calls return a JSON error with a code.",
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


class AgentAuthMiddleware:
    """Rejects MCP requests without a valid agent key; attaches the Agent to the request scope."""

    def __init__(self, app: ASGIApp, db: sqlite3.Connection):
        self._app = app
        self._db = db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            authorization = Request(scope).headers.get("authorization", "")
            api_key = authorization.removeprefix("Bearer ").strip()
            agent = registry.authenticate(self._db, api_key) if api_key else None
            if agent is None or not agent.active:
                response = JSONResponse({"error": "E_AGENT_UNKNOWN", "message": "Missing or invalid agent key."}, 401)
                await response(scope, receive, send)
                return
            scope[AGENT_SCOPE_KEY] = agent
        await self._app(scope, receive, send)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app(db: sqlite3.Connection | None = None, host: str = "127.0.0.1") -> ASGIApp:
    db = db or store.connect()
    server = build_server(db)
    app = server.streamable_http_app(host=host, custom_starlette_routes=[Route("/health", health)])
    return AgentAuthMiddleware(app, db)
