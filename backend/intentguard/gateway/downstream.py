"""The gateway's single long-lived connection to PayPal's MCP server.

PayPal access tokens expire (about 9 hours in sandbox) and the MCP server takes its token at
start-up, so the connection is reopened with a fresh token before the old one expires.

anyio requires a stdio session to be opened and closed in the same task, so one supervisor
task owns the connection; request handlers only wait for it to be ready and call through it.
"""

import time

import anyio
import mcp_types as types
from mcp import ClientSession

from intentguard.config import Settings
from intentguard.paypal.auth import TokenProvider
from intentguard.paypal.mcp_client import paypal_mcp_session


class DownstreamUnavailable(RuntimeError):
    pass


class PayPalDownstream:
    def __init__(self, settings: Settings, tokens: TokenProvider, ready_timeout_s: float = 60):
        self._settings = settings
        self._tokens = tokens
        self._ready_timeout_s = ready_timeout_s
        self._session: ClientSession | None = None
        self._ready = anyio.Event()
        self.tools: dict[str, types.Tool] = {}

    async def run(self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Supervisor loop; start with `task_group.start(downstream.run)`. Returns once first connected."""
        started = False
        while True:
            token = self._tokens.get()
            async with paypal_mcp_session(token, self._settings.paypal_environment) as session:
                listed = await session.list_tools()
                self.tools = {tool.name: tool for tool in listed.tools}
                self._session = session
                self._ready.set()
                if not started:
                    task_status.started()
                    started = True
                await anyio.sleep(max(self._tokens.expires_at - time.time(), 60))
                # Token is about to expire: stop handing out this session, then reconnect.
                self._ready = anyio.Event()
                self._session = None

    async def call_tool(self, name: str, arguments: dict) -> types.CallToolResult:
        with anyio.move_on_after(self._ready_timeout_s):
            await self._ready.wait()
        if self._session is None:
            raise DownstreamUnavailable("PayPal MCP server is not connected")
        return await self._session.call_tool(name, arguments)
