"""Owner API for mandates (under /owner, owner key only).

    GET  /owner/mandates                          each agent, its active mandate and version history
    GET  /owner/mandates/{id}/{version}           one version: rules, example decisions, compiler notes
    POST /owner/mandates/compile                  {agent_id, text} → a checked DRAFT (never active)
    POST /owner/mandates/{id}/{version}/restore   a past version as a new draft (no LLM involved)
    POST /owner/mandates/{id}/{version}/activate  the owner's explicit switch to that draft
"""

import json
import sqlite3
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from intentguard.agents.llm import LLMUnavailable
from intentguard.compiler.mandate import changes_between, compile_mandate, example_decisions, render_rules
from intentguard.gateway import registry
from intentguard.gateway.mandates import MandateError, MandateStore
from intentguard.policy.models import Mandate

MAX_TEXT_CHARS = 2000


def _agents(db: sqlite3.Connection) -> list[registry.Agent]:
    return [registry.get(db, row["id"]) for row in db.execute("SELECT id FROM agents ORDER BY id")]


def _agent_view(agent: registry.Agent) -> dict:
    return {"id": agent.id, "name": agent.name, "purpose": agent.purpose, "status": agent.status,
            "allowed_tools": sorted(agent.allowed_tools)}


def version_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict:
    mandate = Mandate.model_validate_json(row["policy"])
    agent = registry.get(db, row["agent_id"])
    notes = json.loads(row["notes"] or "{}")
    return {
        "mandate_id": row["id"],
        "version": row["version"],
        "agent_id": row["agent_id"],
        "status": row["status"],
        "source_text": row["source_text"],
        "compiled_by": row["compiled_by"],
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
        "activated_by": row["activated_by"],
        "policy": {
            "refunds": mandate.refunds.model_dump(mode="json") if mandate.refunds else None,
            "invoices": mandate.invoices.model_dump(mode="json") if mandate.invoices else None,
        },
        "rules": render_rules(mandate),
        "examples": example_decisions(mandate, agent),
        "warnings": notes.get("warnings", []),
        "blocking": notes.get("blocking", []),
        "unsupported": notes.get("unsupported", []),
        "mapping": notes.get("mapping", []),
        "changes": notes.get("changes", []),
    }


def routes(get_state: Callable[[], object | None]) -> list[Route]:
    def not_ready() -> JSONResponse:
        return JSONResponse({"error": "E_NOT_READY", "message": "The gateway is still starting."}, 503)

    async def overview(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        store = MandateStore(state.db)
        result = []
        for agent in _agents(state.db):
            history = store.history(agent.id)
            active = next((row for row in history if row["status"] == "ACTIVE"), None)
            result.append({
                "agent": _agent_view(agent),
                "active": version_view(state.db, active) if active else None,
                "history": [
                    {"version": row["version"], "status": row["status"], "compiled_by": row["compiled_by"],
                     "created_at": row["created_at"], "activated_at": row["activated_at"],
                     "activated_by": row["activated_by"], "source_text": row["source_text"]}
                    for row in history
                ],
            })
        return JSONResponse(result)

    async def one(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        try:
            version = int(request.path_params["version"])
        except ValueError:
            return JSONResponse({"error": "E_MANDATE_UNKNOWN"}, 404)
        row = MandateStore(state.db).row(request.path_params["mandate_id"], version)
        if row is None:
            return JSONResponse({"error": "E_MANDATE_UNKNOWN"}, 404)
        return JSONResponse(version_view(state.db, row))

    async def compile_text(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        try:
            body = await request.json()
        except ValueError:
            body = None
        agent_id = body.get("agent_id") if isinstance(body, dict) else None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            return JSONResponse({"error": "E_INVALID_REQUEST", "message": f"text is required (max {MAX_TEXT_CHARS} characters)"}, 400)
        agent = registry.get(state.db, agent_id) if isinstance(agent_id, str) else None
        if agent is None:
            return JSONResponse({"error": "E_AGENT_UNKNOWN", "message": "no such agent"}, 404)
        if state.compiler_llm is None:
            return JSONResponse({"error": "E_LLM_UNAVAILABLE", "message": "Set GEMINI_API_KEY to use the compiler."}, 503)

        store = MandateStore(state.db)
        current = store.active(agent.id)
        mandate_id = current.id if current else f"mnd_{agent.id.removesuffix('-agent')}"
        try:
            compiled = await compile_mandate(state.compiler_llm, text, agent.id, mandate_id, agent, current)
        except LLMUnavailable as error:
            return JSONResponse({"error": "E_LLM_UNAVAILABLE", "message": str(error)}, 503)
        if compiled.mandate is None:
            # Not a valid policy: shown to the owner, never stored.
            return JSONResponse({"mandate_id": mandate_id, "version": None, "status": "INVALID", **compiled.notes()})
        version = store.create_draft(compiled.mandate, compiled.model, compiled.notes(), state.clock())
        return JSONResponse(version_view(state.db, store.row(mandate_id, version)))

    async def restore(request: Request) -> JSONResponse:
        """Bring back an earlier version. It was compiled and checked when it was drafted, so it
        becomes a new draft as-is; the owner still has to activate it."""
        state = get_state()
        if state is None:
            return not_ready()
        store = MandateStore(state.db)
        try:
            row = store.row(request.path_params["mandate_id"], int(request.path_params["version"]))
        except ValueError:
            row = None
        if row is None:
            return JSONResponse({"error": "E_MANDATE_UNKNOWN"}, 404)
        past = Mandate.model_validate_json(row["policy"])
        notes = json.loads(row["notes"] or "{}")
        version = store.create_draft(
            past,
            f"restored from v{row['version']}",
            {
                "warnings": notes.get("warnings", []),
                "unsupported": notes.get("unsupported", []),
                "changes": changes_between(store.active(row["agent_id"]), past),
            },
            state.clock(),
        )
        return JSONResponse(version_view(state.db, store.row(row["id"], version)))

    async def activate(request: Request) -> JSONResponse:
        state = get_state()
        if state is None:
            return not_ready()
        mandate_id = request.path_params["mandate_id"]
        try:
            version = int(request.path_params["version"])
            MandateStore(state.db).activate(mandate_id, version, request.scope["intentguard.owner"], state.clock())
        except ValueError as error:  # MandateError is a ValueError
            status = 409 if isinstance(error, MandateError) else 404
            return JSONResponse({"error": "E_CANNOT_ACTIVATE", "message": str(error)}, status)
        return JSONResponse(version_view(state.db, MandateStore(state.db).row(mandate_id, version)))

    return [
        Route("/owner/mandates", overview, methods=["GET"]),
        Route("/owner/mandates/compile", compile_text, methods=["POST"]),
        Route("/owner/mandates/{mandate_id}/{version}", one, methods=["GET"]),
        Route("/owner/mandates/{mandate_id}/{version}/restore", restore, methods=["POST"]),
        Route("/owner/mandates/{mandate_id}/{version}/activate", activate, methods=["POST"]),
    ]
