"""Connection to PayPal's local MCP server, which the gateway uses as its downstream.

The server is the pinned npm package in /paypal-mcp, run directly with node (not npx)
so the version is fixed and Windows doesn't need a .cmd shim.
"""

import os
import shutil
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from intentguard.config import REPO_ROOT

PAYPAL_MCP_ENTRY = REPO_ROOT / "paypal-mcp" / "node_modules" / "@paypal" / "mcp" / "dist" / "index.js"


def server_parameters(access_token: str, environment: str = "SANDBOX") -> StdioServerParameters:
    if not PAYPAL_MCP_ENTRY.exists():
        raise FileNotFoundError(f"{PAYPAL_MCP_ENTRY} not found. Run `npm ci` in paypal-mcp/ first.")
    node = shutil.which("node")
    if node is None:
        raise FileNotFoundError("node is not on PATH (Node 18+ is required for the PayPal MCP server)")
    return StdioServerParameters(
        command=node,
        args=[str(PAYPAL_MCP_ENTRY), "--tools=all"],
        env={
            **os.environ,
            "PAYPAL_ACCESS_TOKEN": access_token,
            "PAYPAL_ENVIRONMENT": environment,
        },
    )


@asynccontextmanager
async def paypal_mcp_session(access_token: str, environment: str = "SANDBOX") -> AsyncIterator[ClientSession]:
    async with stdio_client(server_parameters(access_token, environment)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
