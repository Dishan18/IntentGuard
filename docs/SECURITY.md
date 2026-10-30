# IntentGuard security model

What IntentGuard defends against, how, and where each claim is tested. Everything here runs against
the PayPal **sandbox**; the code refuses any other environment (`config.get_settings`).

## Who is trusted

| Party | Trusted for | Not trusted for |
|---|---|---|
| **The LLM** (inside an agent) | Nothing. Its tool calls are requests. | Amounts, which order, which customer, whether a rule applies |
| **The customer** in a chat | Nothing. Their messages reach the LLM only. | Anything; a message can't set the customer, the agent, or a header |
| **The agent runtime** (store backend, billing worker) | Holding its agent key; stating which logged-in customer a conversation belongs to (`X-IntentGuard-Customer`) | Anything the LLM says |
| **PayPal's API** | The facts: who paid, how much, what's been refunded, refund status | — |
| **The owner** (owner key) | Approving held requests, activating mandates | — |
| **Webhook senders** | Nothing until PayPal confirms the signature, and even then only as "look again" | The payload's contents (status is re-read from PayPal) |

## What is enforced, and where it's tested

**Identity (Know-Your-Agent).** Every MCP request needs an agent key. Only its sha256 is stored, and re-keying revokes the old one. A revoked or unknown key gets 401 before any handler runs. Agent keys and owner keys are separate, so an agent can't approve its own request or touch the owner API, and the owner key can't call tools.
`test_gateway.py::test_key_is_hashed…`, `test_owner.py::test_owner_api_needs_an_owner_key…`, `test_owner_key_is_not_an_agent_key`, `test_console.py::test_console_data_needs_the_owner_key`, `test_mandates.py::test_mandate_api_needs_the_owner_key`

**Scope.** Each agent can only list and call its allowlisted tools. Anything else is `E_TOOL_NOT_ALLOWED`, and no PayPal lookup is spent on it. Scenario D: an over-wired billing agent's refund attempt is refused on identity alone.
`test_gateway.py::test_disallowed_tool…`, `test_security.py::test_an_unknown_tool…`, `test_agents.py::test_compromised_billing_agent…`

**Facts come from PayPal.** Before any refund, the gateway reads the capture from PayPal itself: owner, amount, already refunded, age. The agent's arguments are claims to check. An omitted amount becomes the exact remaining amount, never "refund everything later".
`test_policy_engine.py`, `test_gateway.py::test_allowed_refund_forwards_the_resolved_amount…`

**Session binding.** A refund or order read must belong to the customer the runtime says the conversation is with, so Ben's chat can't touch Carla's order (`E_SUBJECT_MISMATCH`).
`test_gateway.py::test_refund_for_another_customer…`, checkpoint scenario B, red-team "Someone else's order"

**Hostile arguments.** These are all rejected (`E_INVALID_REQUEST`, not sent), and none of them can turn into a full refund:
- amounts that are negative, zero, more than 2 decimals, NaN, infinite, `1e400`, not strings, or the wrong currency;
- malformed `capture_id` values.

The agent can't choose the `invoice_id` that ties a refund to its audit row.
`test_security.py` (hostile amount, amount-object and ID parametrisations, `test_agent_cannot_forge_the_reconciliation_id`)

**IDs can't steer requests.** Anything that isn't a plain PayPal resource ID (`[A-Za-z0-9_-]{1,64}`) is refused before a URL is built, so `../../v1/identity/...` never reaches PayPal with the merchant's token.
`test_security.py::test_ids_from_agents_cant_steer_requests…`, `test_a_path_probe_as_an_order_id…`

**Replays.** An identical money-moving request (refund, invoice create/send/reminder) from the same agent for the same customer within 10 minutes of one that was allowed or queued is refused (`E_DUPLICATE_REQUEST`). The check runs inside the ledger's write lock, so simultaneous copies can't both pass. A request that was blocked, or that PayPal rejected, may be made again. Approving a held request is never mistaken for a replay of itself, and approving twice is refused (409).
`test_security.py` (replay tests, including 5 simultaneous copies), `test_owner.py::test_concurrent_approvals_execute_once`

**Limits under concurrency.** The decision and the ledger reservation happen in one SQLite `BEGIN IMMEDIATE` transaction: 10 simultaneous $40 refunds against a $300 cap let exactly 7 through, across threads and separate connections.
`test_gateway.py::test_concurrent_refunds…`, `test_ledger_reservations_are_atomic_across_connections`

**Fail closed.** If PayPal can't be read for any reason (down, an HTML error page, an unexpected shape, missing credentials), the call is blocked with `E_FACTS_UNAVAILABLE` and audited. The same goes for owner approvals. If PayPal's MCP server is unreachable, nothing is sent and the reservation is released.
`test_security.py` (facts failures, approval failure), `test_gateway.py::test_unreachable_paypal…`

**Unclear outcomes.** If a forwarded call's answer is lost, the money counts as spent (`UNKNOWN`) until the reconciler finds the refund by its call ID at PayPal, or concludes after 10 minutes that it never happened. An agent disconnecting mid-call can't cut the gateway off from recording PayPal's answer. PayPal toolkit errors returned as `{"ok": false}` are recorded as errors, not successes.
`test_reconcile.py`, `test_gateway.py::test_agent_disconnect_mid_refund…`, `test_paypal_error_reported_as_an_ordinary_result…`

**Webhooks.** Each delivery is verified with PayPal's signature-verification API against our webhook ID, and refused if none is configured. Event IDs make deliveries idempotent. The payload is only a pointer: status is re-read from PayPal, so a replayed or tampered event can't change anything.
`test_reconcile.py` (forged, redelivered, payload-vs-API tests)

**Owner approval.** Approval executes the stored request, re-checked with fresh facts under the mandate active now. It covers only the review reasons the owner saw, and reviews expire after 72 hours.
`test_owner.py`

**Mandates.** The LLM only drafts. A limit that doesn't appear in the owner's text blocks activation, an invalid draft is never stored, and only the owner's explicit Activate changes what's enforced. Every audit row records the mandate version it was decided under.
`test_mandates.py`

**Red team.** The dry-run identity is judged like the Support Agent but can never forward, reserve or queue. Its traffic is kept out of the owner's totals.
`test_redteam.py`

## Known limits (demo scope)

- **The store's login is a demo picker.** "Logged in as" sets an unsigned cookie. In a real store, the runtime would take the customer from its own authenticated session; IntentGuard's guarantee is that the *LLM* can't choose the customer, not that the demo login is secure.
- **The agent runtime is trusted to name the customer.** Whoever holds an agent key can claim any customer in `X-IntentGuard-Customer`. That's the boundary by design (the key belongs to the store's backend, never to the LLM); keys are hashed, revocable and scoped to an allowlist and a mandate.
- **Policy allows what the policy allows.** A customer who lies "my mug broke" gets a refund within the auto-approve limit. The red team reports these as "allowed by your rules" so the owner can tighten the mandate.
- **The owner key lives in the browser's sessionStorage** while the console is open. There's no multi-user owner auth or role separation yet.
- **Single SQLite database.** It's correct under concurrency (WAL plus `BEGIN IMMEDIATE`), but it's one instance, not a cluster.
- **The hosted demo needs rate limits** so strangers can't burn the free-tier LLM quota. That's part of the Day 14 deploy work.
