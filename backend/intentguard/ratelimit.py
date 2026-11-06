"""Rate limits for the hosted demo, so a public URL can't burn through the LLM quota or the sandbox.

Sliding windows held in memory (one process, and a restart forgets them, which is fine for a demo).
Only the expensive endpoints are limited: chat turns, mandate compiles, red-team runs and resets.
"""

import json
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Limit:
    method: str
    path: str
    count: int
    per_s: int
    per_ip: bool  # False: one budget shared by everyone
    message: str


HOUR = 3600
DAY = 24 * HOUR

HOSTED_LIMITS = (
    Limit("POST", "/api/chat", 15, 5 * 60, True, "You're chatting faster than the demo allows. Try again in a few minutes."),
    Limit("POST", "/api/chat", 400, DAY, False, "The demo has used today's chat budget. Please try again tomorrow."),
    Limit("POST", "/owner/mandates/compile", 10, 10 * 60, True, "Too many mandate compiles. Try again in a few minutes."),
    Limit("POST", "/owner/mandates/compile", 80, DAY, False, "The demo has used today's compile budget. Try again tomorrow."),
    Limit("POST", "/owner/redteam/runs", 4, HOUR, False, "The red team has run 4 times this hour. Try again later."),
    Limit("POST", "/owner/demo/reset", 6, HOUR, False, "The demo has been reset 6 times this hour. Try again later."),
)


def client_ip(scope: Scope) -> str:
    client = scope.get("client")  # behind Render's proxy, uvicorn fills this from X-Forwarded-For
    return client[0] if client else "unknown"


class RateLimit:
    def __init__(self, app: ASGIApp, limits=HOSTED_LIMITS, clock: Callable[[], float] = time.monotonic):
        self._app = app
        self._limits = limits
        self._clock = clock
        self._hits: dict[tuple[int, str], deque[float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            refused = self._check(scope)
            if refused is not None:
                limit, retry_after = refused
                # "error" carries the sentence too: the store shows a failed chat turn's `error`.
                body = json.dumps({"error": limit.message, "code": "E_RATE_LIMITED", "message": limit.message}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", str(retry_after).encode())],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)

    def _check(self, scope: Scope) -> tuple[Limit, int] | None:
        matching = [
            (i, limit) for i, limit in enumerate(self._limits)
            if scope["method"] == limit.method and scope["path"].rstrip("/") == limit.path
        ]
        if not matching:
            return None
        now = self._clock()
        windows = []
        for i, limit in matching:
            window = self._hits.setdefault((i, client_ip(scope) if limit.per_ip else "*"), deque())
            while window and window[0] <= now - limit.per_s:
                window.popleft()
            if len(window) >= limit.count:
                return limit, max(1, math.ceil(window[0] + limit.per_s - now))
            windows.append(window)
        for window in windows:  # count the request only if every limit lets it through
            window.append(now)
        return None
