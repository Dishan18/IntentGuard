"""How an agent runtime connects to the gateway. The runtime, not the LLM, supplies the agent key
and the authenticated customer for the conversation."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from intentguard.gateway.server import CUSTOMER_HEADER

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765/mcp"


@asynccontextmanager
async def gateway_session(
    api_key: str, customer_id: str | None = None, url: str = DEFAULT_GATEWAY_URL
) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if customer_id:
        headers[CUSTOMER_HEADER] = customer_id
    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
