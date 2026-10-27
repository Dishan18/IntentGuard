# Development log

All work below was done during the PayPal AI Hackathon submission period (Oct 1 – Nov 12, 2026).

## 2026-10-27 — Day 0/1: repo skeleton + PayPal MCP baseline

- Repo created: MIT license, `.env.example`, Python backend package (`backend/intentguard`), pinned PayPal MCP server (`paypal-mcp/`, `@paypal/mcp@1.8.1` → `@paypal/agent-toolkit@1.11.0` via lockfile).
- `intentguard.paypal.auth.TokenProvider`: client-credentials OAuth against the sandbox, cached until 5 min before expiry. `config.get_settings()` refuses any environment other than `SANDBOX`.
- `intentguard.paypal.mcp_client.paypal_mcp_session`: spawns PayPal's MCP server over stdio with `node` (not `npx`) so the version is fixed and it works on Windows.
- `scripts/paypal_tools_list.py`: runs `tools/list` and writes [docs/paypal-mcp-tools.md](docs/paypal-mcp-tools.md) + full JSON schemas. **28 tools** listed.

Findings from the schemas that shape the policy engine:

- `create_refund` requires only `capture_id`; **`amount` is optional and omitting it refunds the full capture.** The gateway must resolve an absent amount to the real remaining capture amount (from PayPal) before checking limits — otherwise "refund without an amount" bypasses every amount rule.
- `send_invoice` / `send_invoice_reminder` accept `additional_recipients` (arbitrary emails). The Billing Agent policy needs a recipient rule, or an injected email could forward invoices anywhere.
- `create_refund.note_to_payer` and invoice `note` are free text sent to customers — treat as output to log, not as anything the policy reads.
- `list_transactions` defaults `start_date` to 31 days before server start; fine for the demo.

Later the same day:

- Sandbox credentials in `.env` work: token minted, authenticated `tools/list` returns the same 28 tools.
- Added `intentguard.paypal.rest.PayPalRest` (orders, captures, refunds) for gateway-side fact lookups and seeding.
- `scripts/seed_orders.py` creates 8 captured orders for 4 demo customers. Each purchase unit carries `custom_id` = the store's customer ID, which the gateway will use to bind refunds to the customer in the chat. Card mode needs no buyer login; `--approve-links` is a PayPal-wallet fallback.
- `scripts/mcp_refund_smoke.py` refunds a seeded capture through PayPal MCP `create_refund` and confirms it via REST.
- **Blocker found:** the default sandbox business account is country IN and returns `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`. Switching the app to a US sandbox business account.
- New sandbox app on a US business account: seeding works — 8 card-paid orders captured (`COMPLETED`) for `cust_ana`, `cust_ben`, `cust_carla`, `cust_dev`.
- **Day 1 done condition met:** `create_refund` through PayPal MCP refunded $5.00 of Ana's $38 capture; REST `GET /v2/payments/refunds/{id}` confirms `COMPLETED`; capture is now `PARTIALLY_REFUNDED`.

More findings for the gateway:

- The `mcp` Python SDK 2.x uses snake_case result fields (`is_error`), and PayPal's `create_refund` returns the refund JSON as a text block.
- A first run crashed on that attribute **after** PayPal had already executed the refund, so Ana's order has two $5 refunds. That's the replay hazard in practice: the gateway must record the decision and reserve the amount *before* forwarding, and treat a client-side error after forwarding as "unknown outcome, reconcile," never "retry."
- `GET /v2/checkout/orders/{id}` returns both `custom_id` (customer binding) and the list of refunds per capture, so one call gives the gateway the facts it needs (owner, captured amount, already refunded). `GET /v2/payments/captures/{id}` alone has neither.

## 2026-10-27 — Day 2: gateway skeleton

- **IntentGuard MCP gateway** (`python -m intentguard.gateway`): an MCP server over streamable HTTP at `/mcp`, built on the `mcp` 2.x low-level `Server`. It holds one long-lived connection to PayPal's MCP server and forwards calls to it.
- **Know-Your-Agent registry** (`gateway/registry.py`): agents have an ID, owner, purpose and tool allowlist; they authenticate with bearer keys stored as sha256 hashes. `scripts/register_agents.py` registers a Support Agent (orders, refunds) and a Billing Agent (invoices only).
- **Per-agent tool surface:** `tools/list` returns only the agent's allowed tools; calling anything else returns `E_TOOL_NOT_ALLOWED` and is never forwarded.
- **Customer binding header:** the agent runtime sends `X-IntentGuard-Customer`; recorded on every call now, enforced by the policy engine on Day 4.
- **Audit log** (SQLite, `tool_calls`): row written *before* forwarding; outcomes `OK`, `PAYPAL_ERROR`, `NOT_SENT`, or `OUTCOME_UNKNOWN` when a failure happens after the request may have reached PayPal (the Day 1 double-refund lesson).
- **Token refresh:** a supervisor task owns the PayPal MCP stdio connection and reopens it with a new token before expiry (anyio requires the session to be opened and closed in the same task).
- `scripts/gateway_smoke.py` against the live sandbox: unknown agent → 401; Support Agent sees 4 tools, `get_order` forwarded, $1 refund on Ben's order `COMPLETED` through the gateway; Billing Agent's $200 refund → `E_TOOL_NOT_ALLOWED`, not sent; all calls in the audit log.
- 6 new offline unit tests (no PayPal needed) for key hashing/re-keying, allowlist filtering, never-forward-on-block, and the `NOT_SENT` / `OUTCOME_UNKNOWN` paths. 8/8 passing.

## 2026-10-27 — Day 3: PayPal facts + deterministic policy engine

- **Policy engine** (`policy/engine.py`): `evaluate(input) -> Decision` is a pure function with no I/O, no clock and no LLM. It runs every applicable check and records each as PASS / FAIL / REVIEW / SKIP with a human-readable detail, so the console can show all the reasons. Decisions are ALLOW, REVIEW or BLOCK. Each error code comes with a hint the agent can relay to the customer.
- **Checks:**
  - Every call: agent active, mandate active and in date, tool allowlisted, tool covered by the mandate.
  - Refunds: request valid, currency matches, facts verified, **customer binding**, amount ≤ what's left on the capture, hard limit, refund window, refunds per customer, daily total, auto-approve threshold.
  - Invoices: recipients must be existing customers, no extra recipients, and total limits. The review threshold applies when an invoice is *sent*, since a draft moves nothing.
  - `get_order` / `get_refund` reads are customer-bound too, so the Support Agent can't read another customer's order.
- **Amounts come from PayPal, not the agent:**
  - An omitted refund amount resolves to the full remaining amount and goes through every limit.
  - A malformed amount (negative, `NaN`, 3 decimals, wrong shape) is blocked and never treated as "refund everything".
  - Invoice totals are upper bounds: items × quantity + tax + shipping, with discounts ignored.
- **Facts** (`gateway/facts.py`): direct REST reads the agent can't influence. A capture gives the owner (`custom_id`), amount and date; the order gives refunds, filtered to this capture and to COMPLETED/PENDING. Refund → its capture, order → owner, invoice → recipients and total. Lookups never raise: 404 / `INVALID_RESOURCE_ID` → `E_UNKNOWN_*`; any other failure → `E_FACTS_UNAVAILABLE` (fail closed).
- **Mandate model** (`policy/models.py`, pydantic): refund and invoice sections with validated thresholds. The demo mandates in `demo.py` are the owner's rules from the spec.
- **Correction to Day 1:** `GET /v2/payments/captures/{id}` *does* return `custom_id`, plus an `up` link to the order. Facts use the capture for owner/amount/date and the order for the refund list.
- `list_transactions` removed from the Support Agent: it returns every customer's payments and can't be customer-bound.
- **Tests: 60 passing.** That's 40 engine tests (each demo scenario, amount resolution, fail-closed facts, every limit, invoice rules), 9 facts parsing tests, and a Hypothesis property test (400 cases per run) asserting that a non-BLOCK refund always belongs to the session customer, is within what PayPal says is refundable, and is within the hard, daily, frequency and age limits; and that ALLOW is always within auto-approve.
- `scripts/policy_smoke.py` evaluates the demo scenarios against **live sandbox data** (read-only):

  ```text
  A  Ana: $20 for broken mug         ALLOW  amount=$20.00 (PayPal: $28.00 refundable)
  B  Ben: injected $900 refund       BLOCK  E_EXCEEDS_CAPTURE, E_ABOVE_HARD_LIMIT, E_DAILY_LIMIT
  B  Ben: refund Carla's order       BLOCK  E_SUBJECT_MISMATCH
  C  Carla: full $160 dinner set     REVIEW R_ABOVE_AUTO_LIMIT
  -  Dev: refund, no amount given    BLOCK  E_ABOVE_HARD_LIMIT amount=$240.00
  -  Ben: look up Dev's order        BLOCK  E_SUBJECT_MISMATCH
  -  Ana: made-up capture id         BLOCK  E_UNKNOWN_CAPTURE
  ```

Next (Day 4): wire the engine into the gateway, add a ledger with atomic reservations (per-customer counts and daily totals), and return structured errors to agents.
