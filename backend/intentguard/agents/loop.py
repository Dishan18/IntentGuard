"""The tool-use loop shared by the demo agents: model turn → tool calls through the gateway →
results back to the model, until it answers in text or runs out of steps.

The loop only sees what the gateway returns. A BLOCK or REVIEW comes back as a result like any
other; deciding what may happen is the gateway's job, not the agent's.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mcp import ClientSession

from intentguard.agents.llm import GeminiClient, LLMUnavailable

log = logging.getLogger("intentguard.agents")

ToolStartListener = Callable[[str, dict], Awaitable[None]]


@dataclass
class ToolEvent:
    tool: str
    arguments: dict
    decision: str  # ALLOW | REVIEW | BLOCK, or ERROR when PayPal itself failed
    code: str | None
    result: dict | str


ToolEventListener = Callable[[ToolEvent], Awaitable[None]]


@dataclass
class AgentReply:
    text: str
    model: str
    events: list[ToolEvent] = field(default_factory=list)
    degraded: bool = False  # the LLM failed; text was written from the gateway's results instead


def parse_result(result) -> tuple[str, str | None, dict | str]:
    text = "".join(block.text for block in result.content if block.type == "text")
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    if isinstance(body, dict) and body.get("decision") in ("BLOCK", "REVIEW"):
        return body["decision"], body.get("code"), body
    # PayPal's toolkit reports API errors as `{"ok": false, ...}` rather than as an MCP error.
    paypal_error = result.is_error or (isinstance(body, dict) and body.get("ok") is False)
    return ("ERROR" if paypal_error else "ALLOW"), None, body


def text_of(content: dict) -> str:
    return "".join(part.get("text", "") for part in content["parts"] if not part.get("thought")).strip()


async def run_tool_loop(
    llm: GeminiClient,
    session: ClientSession,
    system: str,
    contents: list[dict],
    tools: list[dict],
    max_tool_steps: int,
    fallback: Callable[[list[ToolEvent]], str],
    gave_up: str,
    on_tool_start: ToolStartListener | None = None,
    on_tool_event: ToolEventListener | None = None,
) -> AgentReply:
    """Run one agent turn. `fallback` writes the reply from the gateway's results if the LLM fails
    mid-turn; `gave_up` is the reply when the step limit is reached."""
    events: list[ToolEvent] = []
    model = None
    for _ in range(max_tool_steps + 1):
        try:
            generation = await llm.generate(system, contents, tools, pin_model=model)
        except LLMUnavailable as error:
            log.warning("LLM unavailable mid-turn (model %s, %d tool calls so far): %s", model, len(events), error)
            return AgentReply(fallback(events), model or "", events, degraded=True)
        model = generation.model
        contents.append(generation.content)
        calls = [part["functionCall"] for part in generation.content["parts"] if "functionCall" in part]
        if not calls:
            return AgentReply(text_of(generation.content), model, events)

        responses = []
        for call in calls:
            arguments = call.get("args") or {}
            if on_tool_start:
                await on_tool_start(call["name"], arguments)
            result = await session.call_tool(call["name"], arguments)
            decision, code, body = parse_result(result)
            event = ToolEvent(call["name"], arguments, decision, code, body)
            events.append(event)
            if on_tool_event:
                await on_tool_event(event)
            function_response = {
                "name": call["name"],
                "response": {"error": body} if result.is_error or decision == "ERROR" else {"output": body},
            }
            if "id" in call:
                function_response["id"] = call["id"]
            responses.append({"functionResponse": function_response})
        contents.append({"role": "user", "parts": responses})

    return AgentReply(gave_up, model or "", events)
