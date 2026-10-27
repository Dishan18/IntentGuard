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
