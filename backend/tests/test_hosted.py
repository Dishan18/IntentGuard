"""The hosted deployment: one port for gateway + store, rate limits, the fixed owner key, and the
owner's Reset demo endpoint."""

from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from intentguard import store
from intentguard.gateway import demo_routes, registry
from intentguard.gateway.app import AuthMiddleware
from intentguard.hosted import Router, is_gateway_path
from intentguard.ratelimit import Limit, RateLimit


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(tmp_path):
    return store.connect(tmp_path / "test.db")


def client(app, ip="203.0.113.7"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1234)), base_url="http://demo.test")


def named(name):
    async def endpoint(request):
        return PlainTextResponse(name)

    return Starlette(routes=[Route("/{path:path}", endpoint, methods=["GET", "POST"])])


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/owner/summary", "/console/", "/console", "/webhooks/paypal", "/health"])
def test_gateway_paths_go_to_the_gateway(path):
    assert is_gateway_path(path)


@pytest.mark.parametrize("path", ["/", "/api/chat", "/api/session", "/assets/index.js", "/ownerx", "/healthz", "/mcpx"])
def test_everything_else_goes_to_the_store(path):
    assert not is_gateway_path(path)


@pytest.mark.anyio
async def test_router_sends_requests_by_path():
    async with client(Router(named("gateway"), named("store"))) as http:
        assert (await http.get("/owner/summary")).text == "gateway"
        assert (await http.post("/api/chat")).text == "store"
        assert (await http.get("/")).text == "store"


LIMITS = (
    Limit("POST", "/api/chat", 2, 60, True, "slow down"),
    Limit("POST", "/api/chat", 3, 3600, False, "budget used"),
)


@pytest.mark.anyio
async def test_rate_limit_per_ip_then_shared_budget():
    now = [0.0]
    app = RateLimit(named("store"), LIMITS, clock=lambda: now[0])
    async with client(app, "198.51.100.1") as first, client(app, "198.51.100.2") as second:
        assert (await first.post("/api/chat")).status_code == 200
        assert (await first.post("/api/chat")).status_code == 200
        refused = await first.post("/api/chat")
        assert refused.status_code == 429
        assert refused.json()["error"] == "slow down"  # the store shows `error` in the chat
        assert refused.json()["code"] == "E_RATE_LIMITED"
        assert 1 <= int(refused.headers["retry-after"]) <= 60

        # Another visitor has their own per-IP window, but the shared budget (3) runs out.
        assert (await second.post("/api/chat")).status_code == 200
        assert (await second.post("/api/chat")).json()["error"] == "budget used"


@pytest.mark.anyio
async def test_refused_requests_dont_use_up_the_budget_and_windows_slide():
    now = [0.0]
    app = RateLimit(named("store"), LIMITS, clock=lambda: now[0])
    async with client(app) as http:
        for _ in range(2):
            assert (await http.post("/api/chat")).status_code == 200
        for _ in range(5):
            assert (await http.post("/api/chat")).status_code == 429  # per-IP; not counted against the shared 3
        now[0] = 61
        assert (await http.post("/api/chat")).status_code == 200  # the third of the shared budget


@pytest.mark.anyio
async def test_unlimited_paths_and_methods_pass_through():
    app = RateLimit(named("store"), (Limit("POST", "/api/chat", 1, 60, True, "x"),), clock=lambda: 0.0)
    async with client(app) as http:
        for _ in range(5):
            assert (await http.get("/api/chat")).status_code == 200
            assert (await http.post("/api/session")).status_code == 200


def test_owner_key_from_the_environment_is_used_as_given(db):
    key = registry.register_owner(db, "Maya", "  a-long-enough-hosted-owner-key  ")
    assert key == "a-long-enough-hosted-owner-key"
    assert registry.authenticate_owner(db, key) == "Maya"
    # Re-registering on the next start replaces it rather than adding a second key.
    registry.register_owner(db, "Maya", "another-long-enough-owner-key")
    assert registry.authenticate_owner(db, key) is None


def test_owner_key_must_be_long_enough(db):
    with pytest.raises(ValueError):
        registry.register_owner(db, "Maya", "short")


def reset_app(db, reset):
    state = SimpleNamespace(db=db, shadows={"attack-1": object()})
    app = AuthMiddleware(Starlette(routes=demo_routes.routes(lambda: state, reset=reset)), db)
    return app, state


@pytest.mark.anyio
async def test_reset_needs_the_owner_key_and_reseeds(db):
    calls = []

    def fake_reset(say):
        calls.append(say)
        return [{"order_id": "O1"}, {"order_id": "O2"}]

    app, state = reset_app(db, fake_reset)
    owner_key = registry.register_owner(db, "Maya")
    agent_key = registry.register(db, "support-agent", "Support", "o", "p", ["create_refund"])
    async with client(app) as http:
        assert (await http.post("/owner/demo/reset")).status_code == 401
        assert (await http.post("/owner/demo/reset", headers={"Authorization": f"Bearer {agent_key}"})).status_code == 401
        assert not calls

        response = await http.post("/owner/demo/reset", headers={"Authorization": f"Bearer {owner_key}"})
    assert response.status_code == 200
    assert response.json()["orders"] == 2
    assert len(calls) == 1
    assert state.shadows == {}  # red-team shadow ledgers belong to the old orders


@pytest.mark.anyio
async def test_reset_waits_for_a_red_team_run(db):
    db.execute("INSERT INTO redteam_runs (id, started_at, status, compromised, total) VALUES ('r1', 'now', 'RUNNING', 1, 3)")
    app, _ = reset_app(db, lambda say: pytest.fail("reset while the red team is running"))
    owner_key = registry.register_owner(db, "Maya")
    async with client(app) as http:
        response = await http.post("/owner/demo/reset", headers={"Authorization": f"Bearer {owner_key}"})
    assert response.status_code == 409
    assert response.json()["error"] == "E_RUN_IN_PROGRESS"


@pytest.mark.anyio
async def test_a_failed_reset_says_so(db):
    def broken(say):
        raise RuntimeError("PayPal is down")

    app, _ = reset_app(db, broken)
    owner_key = registry.register_owner(db, "Maya")
    async with client(app) as http:
        response = await http.post("/owner/demo/reset", headers={"Authorization": f"Bearer {owner_key}"})
    assert response.status_code == 502
    assert response.json()["error"] == "E_RESET_FAILED"
