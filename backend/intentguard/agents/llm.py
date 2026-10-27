"""Gemini client for agents: function calling over the REST API, with retries and model fallback.

Kept deliberately small and provider-shaped (system text, contents, tool declarations in; a model
"content" out) so another provider can be dropped in behind the same `generate()` call.

Fallback to the next model happens only on the first request of a turn (`pin_model=None`). Once a
turn has started tool calls it stays on one model: Gemini's thought signatures are per-model, and
restarting a turn on another model could repeat a tool call that already moved money.
"""

import asyncio
import os
from dataclasses import dataclass

import httpx

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Overloaded or rate limited: worth retrying. Anything else (bad request, auth) is not.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class Generation:
    content: dict  # {"role": "model", "parts": [...]}, to append to the conversation verbatim
    model: str


def tool_declaration(name: str, description: str | None, input_schema: dict) -> dict:
    """An MCP tool as a Gemini function declaration. parametersJsonSchema takes MCP's JSON Schema as-is."""
    return {
        "name": name,
        "description": " ".join((description or "").split())[:1000],
        "parametersJsonSchema": input_schema,
    }


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        models: list[str],
        attempts_per_model: int = 2,
        backoff_s: float = 2.0,
        timeout_s: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set. Add it to .env (see .env.example).")
        if not models:
            raise LLMUnavailable("no Gemini model configured")
        self._api_key = api_key
        self.models = models
        self._attempts = attempts_per_model
        self._backoff_s = backoff_s
        self._timeout_s = timeout_s
        self._transport = transport

    @classmethod
    def from_env(cls) -> "GeminiClient":
        primary = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        fallbacks = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",") if m.strip()]
        return cls(os.getenv("GEMINI_API_KEY", ""), [primary, *[m for m in fallbacks if m != primary]])

    async def generate(
        self, system: str, contents: list[dict], tools: list[dict], pin_model: str | None = None
    ) -> Generation:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.2, "thinkingConfig": {"thinkingLevel": "low"}},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]

        errors = []
        async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
            for model in [pin_model] if pin_model else self.models:
                for attempt in range(self._attempts):
                    try:
                        response = await client.post(
                            f"{API_BASE}/models/{model}:generateContent",
                            headers={"x-goog-api-key": self._api_key},
                            json=body,
                        )
                    except httpx.TransportError as error:
                        errors.append(f"{model}: {type(error).__name__}")
                    else:
                        if response.is_success:
                            return Generation(self._content(response.json(), model), model)
                        message = response.json().get("error", {}).get("message", response.text)[:200]
                        errors.append(f"{model}: {response.status_code} {message}")
                        if response.status_code not in RETRYABLE_STATUS:
                            raise LLMUnavailable("; ".join(errors))
                    if attempt + 1 < self._attempts:
                        await asyncio.sleep(self._backoff_s * (2**attempt))
        raise LLMUnavailable("; ".join(errors))

    @staticmethod
    def _content(body: dict, model: str) -> dict:
        candidates = body.get("candidates") or []
        if not candidates or "content" not in candidates[0]:
            reason = (candidates[0].get("finishReason") if candidates else None) or body.get("promptFeedback")
            raise LLMUnavailable(f"{model} returned no content ({reason})")
        content = candidates[0]["content"]
        content.setdefault("role", "model")
        content.setdefault("parts", [])
        return content
