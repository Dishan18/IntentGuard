# IntentGuard: API reference

Every HTTP endpoint, who may call it and what it returns.

- **Hosted base URL:** `https://intentguard-5dd9.onrender.com`. Everything is on one origin.
- **Local:** the gateway is on `http://127.0.0.1:8765` and the store on `http://127.0.0.1:8766`. In Docker, both are on `http://localhost:10000`.

A runnable version of most of this is the Postman collection in [`docs/postman`](postman/IntentGuard.postman_collection.json).

## Authentication

| Credential | Header | Works on | Get it from |
|---|---|---|---|
| **Agent key** | `Authorization: Bearer <key>` | `/mcp` only | `data/agent_keys.json` (local), `INTENTGUARD_SUPPORT_AGENT_KEY` (hosted) |
| **Session customer** | `X-IntentGuard-Customer: cust_ben` | `/mcp` | Set by the agent runtime from its own login, never by the LLM |
| **Owner key** | `Authorization: Bearer <key>` | `/owner/*` only | `data/agent_keys.json` → `owner` (local), `INTENTGUARD_OWNER_KEY` (hosted) |
| **Store cookie** | `ig_demo_customer` | `/api/*` | `POST /api/session` |
| **PayPal signature** | PayPal transmission headers | `/webhooks/paypal` | PayPal |

An agent key on `/owner` gets **401**, and so does an owner key on `/mcp`.

## Errors

The gateway returns JSON errors like `{"error": "E_CODE", "message": "..."}`. Rate-limited requests (hosted only) get **429** with a `Retry-After` header and this body:

```json
{"error": "<sentence>", "code": "E_RATE_LIMITED", "message": "<sentence>"}
```

---

## Agents: MCP at `/mcp`

This is MCP over streamable HTTP (JSON-RPC 2.0). Send `Accept: application/json, text/event-stream`. Answers come as server-sent events (`data: {...}`).

1. **`initialize`** returns a `mcp-session-id` header. Send it on every later call.
2. **`notifications/initialized`** returns 202.
3. **`tools/list`** returns only the tools in this agent's allowlist (Support Agent: `create_refund`, `get_order`, `get_refund`).
4. **`tools/call`** runs `{name, arguments}` through IntentGuard.

### Tool call results

The tool result's `content[0].text` is JSON.

**BLOCK** (`isError: true`), nothing sent to PayPal:

```json
{
  "decision": "BLOCK",
  "code": "E_EXCEEDS_CAPTURE",
  "codes": ["E_EXCEEDS_CAPTURE", "E_ABOVE_HARD_LIMIT", "E_DAILY_LIMIT"],
  "message": "The amount is more than what is left to refund on this payment.",
  "reasons": [{"rule": "within_capture", "code": "E_EXCEEDS_CAPTURE", "detail": "$900.00 requested, $42.00 left of $42.00 captured"}],
  "retryable": false,
  "paypal_call": "NOT_SENT",
  "call_id": "call_..."
}
```

**REVIEW**, held for the owner:

```json
{"decision": "REVIEW", "review_id": "rv_...", "message": "This needs the owner's approval...", "paypal_call": "NOT_SENT_YET", "call_id": "call_..."}
```

**ALLOW:** PayPal's own result, passed through. For a refund, this includes its `id` and `status`.

### Tool arguments (Support Agent)

| Tool | Arguments |
|---|---|
| `get_order` | `{"id": "<order id>"}` |
| `get_refund` | `{"refund_id": "<refund id>"}` |
| `create_refund` | `{"capture_id": "<capture id>", "amount": {"currency_code": "USD", "value": "10.00"}}`. Omit `amount` to refund exactly what's left. `value` must be a string. |

Every code is listed in [lld.md §2.4](lld.md#24-codes).

---

## Owner API (`/owner/*`, owner key)

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /owner/summary` | — | Today's KPIs: refunded automatically, owner-approved, held, blocked, money protected, daily cap used, calls today |
| `GET /owner/decisions` | `?limit=` | Recent agent actions (red-team dry runs excluded), each with `decision`, `codes`, `paypal_call`, `summary` sentence, review status |
| `GET /owner/decisions/{call_id}` | — | One decision: every check, the mandate version it was decided under, arguments, PayPal's response, ledger entry, review, timeline (including webhooks) |
| `GET /owner/stream` | — | SSE `event: snapshot` with `{summary, decisions, reviews}` whenever it changes; keepalive every 15 s |
| `GET /owner/reviews` | `?status=PENDING` | Held requests with their reasons |
| `POST /owner/reviews/{id}/approve` | `{"note": "..."}` | Re-checks with fresh PayPal facts, then executes: `{status: APPROVED, paypal_call: SENT, paypal}`. If the re-check fails: `{status: FAILED, codes, paypal_call: NOT_SENT}`. Already decided: 409 |
| `POST /owner/reviews/{id}/reject` | `{"note": "..."}` | `{status: REJECTED}` |
| `GET /owner/mandates` | — | Each agent with its active mandate (rules, examples) and version history |
| `GET /owner/mandates/{id}/{version}` | — | One version: rules, example decisions, compiler notes, changes |
| `POST /owner/mandates/compile` | `{"agent_id": "support-agent", "text": "..."}` (≤ 2,000 chars) | A checked **DRAFT**: `rules`, `examples`, `warnings`, `blocking`, `unsupported`, `mapping`, `changes`. Never active. |
| `POST /owner/mandates/{id}/{version}/activate` | — | Makes that draft ACTIVE; the previous version becomes SUPERSEDED. Refused if it has blocking problems. |
| `POST /owner/mandates/{id}/{version}/restore` | — | Copies an earlier version into a new draft (no LLM) |
| `POST /owner/redteam/runs` | `{"compromised": true, "generated": 0-5}` | 202 `{run_id, total}`; the run continues in the background. 409 if one is already running. |
| `GET /owner/redteam/runs/latest` | — | The latest run's report; results appear while it runs |
| `GET /owner/redteam/runs/{run_id}` | — | One run's report |
| `POST /owner/demo/reset` | — | Fresh sandbox orders, built-in mandates, empty log, reviews and red-team history: `{orders, message}`. 409 while a red-team run or another reset is going. |

## Store API (`/api/*`, cookie)

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/customers` | — | `[{id, name}]`, the four demo customers |
| `POST /api/session` | `{"customer_id": "cust_ben"}` | Sets the session cookie: `{id, name}` |
| `GET /api/session` | — | `{customer, orders: [{order_id, item, amount, refunded, refund_processing, refund_failed, review}], suggestions}` |
| `POST /api/chat` | `{"message": "...", "history": [{role: user\|model, text}], "compromised": false}` | SSE: `tool_start` `{tool, amount}`, `tool` `{tool, amount, decision, codes?, message?, paypal_call?, paypal_status?, paypal_id?, review_id?}`, `reply` `{text, model, degraded}`, `error` `{message}` |

## Other

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | `{"status": "ok"}` |
| `POST /webhooks/paypal` | PayPal signature | Refund events. Verified with PayPal, deduplicated by event ID, then reconciled from PayPal's API (the payload is only a pointer). Without a configured `PAYPAL_WEBHOOK_ID`, every delivery is refused (503). |
| `GET /console/` | none (data needs the owner key) | The owner console SPA |
| `GET /` | none | The store SPA |
