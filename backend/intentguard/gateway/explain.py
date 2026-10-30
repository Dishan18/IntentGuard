"""One plain sentence per decision, for the console. Built from the recorded rule results, never
by an LLM and never fed back into a decision: it describes what the rules did, nothing more."""

from decimal import Decimal, InvalidOperation

AGENT_NAMES = {"support-agent": "the Support Agent", "billing-agent": "the Billing Agent"}

# What each blocking code means, as the end of "…, but <reason>".
REASONS = {
    "E_EXCEEDS_CAPTURE": "that's more than is left to refund on the payment",
    "E_ABOVE_HARD_LIMIT": "it's above your hard limit",
    "E_DAILY_LIMIT": "it would go over today's refund cap",
    "E_SUBJECT_MISMATCH": "the order belongs to a different customer than the one in the chat",
    "E_FREQUENCY": "this customer already had their refund for the period",
    "E_OUTSIDE_WINDOW": "the order is older than your refund window",
    "E_TOOL_NOT_ALLOWED": "this agent isn't allowed to use that PayPal tool",
    "E_NOT_IN_MANDATE": "your mandate for this agent doesn't cover it",
    "E_MANDATE_INACTIVE": "this agent has no active mandate",
    "E_AGENT_INACTIVE": "this agent has been switched off",
    "E_UNKNOWN_RECIPIENT": "the recipient isn't one of your customers",
    "E_UNAPPROVED_RECIPIENT": "it adds recipients you haven't allowed",
    "E_UNKNOWN_CAPTURE": "PayPal has no such payment",
    "E_UNKNOWN_ORDER": "PayPal has no such order",
    "E_UNKNOWN_REFUND": "PayPal has no such refund",
    "E_UNKNOWN_INVOICE": "PayPal has no such invoice",
    "E_FACTS_UNAVAILABLE": "PayPal couldn't be reached to check the facts, so it failed closed",
    "E_CURRENCY_NOT_ALLOWED": "the currency isn't allowed",
    "E_INVALID_REQUEST": "the request was malformed",
    "E_REVIEW_CHANGED": "it needed approval for a new reason by the time you approved it",
    "E_DUPLICATE_REQUEST": "it was an exact repeat of a request made moments before",
}


def _money(value: str | None) -> str:
    try:
        return f"${Decimal(value):,.2f}"
    except (InvalidOperation, TypeError):
        return ""


def _action(row: dict) -> str:
    amount = _money(row.get("amount"))
    customer = row.get("customer_name") or row.get("customer_id")
    tool = row["tool"]
    if tool == "create_refund":
        return f"refund {amount or 'a payment'}" + (f" to {customer}" if customer else "")
    if tool == "send_invoice":
        return f"send a {amount} invoice" if amount else "send an invoice"
    if tool == "create_invoice":
        return f"create a {amount} invoice" if amount else "create an invoice"
    if tool == "send_invoice_reminder":
        return "send an invoice reminder"
    if tool in ("get_order", "get_refund", "get_invoice", "list_invoices"):
        return "look up " + {"get_order": "an order", "get_refund": "a refund", "get_invoice": "an invoice"}.get(tool, "invoices")
    return f"call {tool}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def explain(row: dict, checks: list[dict]) -> str:
    """`row` is a console decision row (decision, tool, amount, customer, codes, status...)."""
    actor = AGENT_NAMES.get(row.get("agent_id") or "", row.get("agent_id") or "An agent")
    action = _action(row)
    decision = row["decision"]
    status = row.get("status")

    if decision == "BLOCK":
        codes = [code for code in row.get("codes") or [] if code.startswith("E_")]
        reasons = [REASONS.get(code, code) for code in codes] or ["a rule failed"]
        return f"Stopped before PayPal: {actor} tried to {action}, but {_join(reasons)}."
    if decision == "REVIEW":
        detail = next((c["detail"] for c in checks if c.get("result") == "REVIEW"), "it needs your approval")
        outcome = {
            "AWAITING_OWNER": "Waiting for you",
            "APPROVED": "You approved it",
            "REJECTED": "You rejected it",
        }.get(status, "Held for you")
        return f"{outcome}: {actor} asked to {action}, and {detail}."
    # ALLOW
    if status in ("PAYPAL_ERROR",):
        return f"Allowed, but PayPal rejected it: {actor} tried to {action}."
    if status == "IN_FLIGHT":
        return f"Allowed and sent to PayPal: {actor} asked to {action}; waiting for PayPal's answer."
    if status == "UNKNOWN":
        return f"Sent to PayPal but the answer was lost: {actor} tried to {action}; the reconciler is checking PayPal."
    if status in ("FAILED", "CANCELLED", "NOT_FOUND"):
        return f"Allowed, but the refund didn't go through at PayPal ({status.lower()})."
    how = "after your approval" if row.get("owner_approved") else "within your rules"
    if row["tool"] == "create_refund":
        settled = "is still processing" if status == "PENDING" else "was completed"
        return f"Done {how}: PayPal refund {_money(row.get('amount'))}" + (
            f" to {row.get('customer_name')}" if row.get("customer_name") else ""
        ) + f" {settled}."
    return f"Done {how}: {actor} was allowed to {action}."
