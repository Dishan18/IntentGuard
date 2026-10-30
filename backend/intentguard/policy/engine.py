"""Deterministic policy engine.

`evaluate()` is a pure function: no I/O, no clock, no randomness, no LLM. The same input always
produces the same Decision. It runs every applicable check (not just until the first failure) so
the console can show all the reasons a call was blocked, and records each one.

Money-moving amounts are taken from PayPal's facts when the agent leaves them out (an omitted
refund amount means "refund everything left"), so leaving a field blank can't skip a limit.
"""

from decimal import Decimal, InvalidOperation

from intentguard.policy.models import (
    CaptureFacts,
    Check,
    CheckResult,
    Decision,
    EvaluationInput,
    FactsFailure,
    InvoiceFacts,
    OrderFacts,
    Outcome,
)

CENT = Decimal("0.01")

REFUND_TOOLS = {"create_refund", "get_refund", "get_order"}
INVOICE_TOOLS = {
    "create_invoice",
    "send_invoice",
    "send_invoice_reminder",
    "get_invoice",
    "list_invoices",
    "cancel_sent_invoice",
}

AGENT_HINTS = {
    "E_AGENT_INACTIVE": "This agent has been disabled by the owner.",
    "E_MANDATE_INACTIVE": "There is no active authorization for this action. Tell the customer a person will follow up.",
    "E_TOOL_NOT_ALLOWED": "You are not permitted to use this tool.",
    "E_NOT_IN_MANDATE": "The owner has not authorized this kind of action for you.",
    "E_INVALID_REQUEST": "The request is malformed. Check the arguments and try again.",
    "E_CURRENCY_NOT_ALLOWED": "Only the mandate's currency is allowed.",
    "E_FACTS_UNAVAILABLE": "Payment details could not be verified right now. Nothing was done; try again later.",
    "E_UNKNOWN_CAPTURE": "That payment does not exist. Look up the customer's order first.",
    "E_UNKNOWN_INVOICE": "That invoice does not exist.",
    "E_NO_SESSION_CUSTOMER": "There is no verified customer in this conversation.",
    "E_SUBJECT_MISMATCH": "You can only act on orders that belong to the customer you are talking to.",
    "E_EXCEEDS_CAPTURE": "The amount is more than what is left to refund on this payment.",
    "E_ABOVE_HARD_LIMIT": "This amount is above what you may do, even with approval. Tell the customer a person will review it.",
    "E_OUTSIDE_WINDOW": "This order is outside the refund window.",
    "E_FREQUENCY": "This customer has reached the refund limit for this period.",
    "E_DAILY_LIMIT": "Today's refund budget is used up. Tell the customer a person will follow up.",
    "E_UNKNOWN_RECIPIENT": "Invoices can only go to existing customers.",
    "E_UNAPPROVED_RECIPIENT": "You may not add extra recipients.",
    "R_ABOVE_AUTO_LIMIT": "This needs the owner's approval. Tell the customer the request is pending review.",
}


class _Checks:
    def __init__(self):
        self.items: list[Check] = []

    def ok(self, rule: str, detail: str) -> None:
        self.items.append(Check(rule, CheckResult.PASS, detail))

    def fail(self, rule: str, code: str, detail: str) -> None:
        self.items.append(Check(rule, CheckResult.FAIL, detail, code))

    def review(self, rule: str, code: str, detail: str) -> None:
        self.items.append(Check(rule, CheckResult.REVIEW, detail, code))

    def skip(self, rule: str, detail: str) -> None:
        self.items.append(Check(rule, CheckResult.SKIP, detail))

    @property
    def failed(self) -> bool:
        return any(check.result is CheckResult.FAIL for check in self.items)


def _money(value) -> Decimal:
    """Parse a PayPal amount string. Raises ValueError for anything that isn't a positive 2-dp amount."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise ValueError(f"not a number: {value!r}") from error
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise ValueError(f"not a positive amount with at most 2 decimals: {value!r}")
    return amount


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def evaluate(inp: EvaluationInput) -> Decision:
    checks = _Checks()
    _gate(inp, checks)
    resolved: Decimal | None = None
    if not checks.failed:
        handler = _HANDLERS.get(inp.tool)
        if handler is not None:
            resolved = handler(inp, checks)
    return _decide(checks, resolved)


def _gate(inp: EvaluationInput, checks: _Checks) -> None:
    if inp.agent.active:
        checks.ok("agent_active", f"agent {inp.agent.id} is registered and active")
    else:
        checks.fail("agent_active", "E_AGENT_INACTIVE", f"agent {inp.agent.id} is disabled")

    mandate = inp.mandate
    if mandate is None or mandate.agent_id != inp.agent.id:
        checks.fail("mandate_active", "E_MANDATE_INACTIVE", "no mandate for this agent")
    elif mandate.status != "ACTIVE":
        checks.fail("mandate_active", "E_MANDATE_INACTIVE", f"mandate {mandate.id} is {mandate.status}")
    elif mandate.valid_from and inp.now < mandate.valid_from:
        checks.fail("mandate_active", "E_MANDATE_INACTIVE", f"mandate {mandate.id} starts {mandate.valid_from:%Y-%m-%d}")
    elif mandate.expires_at and inp.now >= mandate.expires_at:
        checks.fail("mandate_active", "E_MANDATE_INACTIVE", f"mandate {mandate.id} expired {mandate.expires_at:%Y-%m-%d}")
    else:
        checks.ok("mandate_active", f"mandate {mandate.id} v{mandate.version} is active")

    if inp.tool not in inp.agent.allowed_tools:
        checks.fail("tool_allowed", "E_TOOL_NOT_ALLOWED", f"{inp.tool} is not in this agent's allowlist")
        return
    checks.ok("tool_allowed", f"{inp.tool} is in this agent's allowlist")

    if mandate is None:
        return
    if inp.tool in REFUND_TOOLS and mandate.refunds is None:
        checks.fail("in_mandate", "E_NOT_IN_MANDATE", "the mandate does not cover refunds")
    elif inp.tool in INVOICE_TOOLS and mandate.invoices is None:
        checks.fail("in_mandate", "E_NOT_IN_MANDATE", "the mandate does not cover invoices")
    elif inp.tool not in REFUND_TOOLS | INVOICE_TOOLS:
        checks.fail("in_mandate", "E_NOT_IN_MANDATE", f"IntentGuard has no policy for {inp.tool}")


def _describe_facts(facts) -> str:
    """The facts in one line, for the audit trail."""
    if isinstance(facts, CaptureFacts):
        return (
            f"capture {facts.capture_id}, ${facts.amount:.2f} {facts.currency} captured, "
            f"${facts.already_refunded:.2f} already refunded, customer {facts.customer_id or 'unknown'}"
        )
    if isinstance(facts, InvoiceFacts):
        return f"invoice {facts.invoice_id}, {facts.status}, ${facts.total:.2f} {facts.currency}"
    if isinstance(facts, OrderFacts):
        return f"order {facts.order_id}, customer {facts.customer_id or 'unknown'}"
    return type(facts).__name__


def _facts_check(inp: EvaluationInput, checks: _Checks, expected: type, not_found_code: str):
    """Returns the facts if usable; otherwise records why not (failing closed) and returns None."""
    facts = inp.facts
    if isinstance(facts, expected):
        checks.ok("facts", f"read from PayPal: {_describe_facts(facts)}")
        return facts
    if isinstance(facts, FactsFailure) and facts.not_found:
        checks.fail("facts", not_found_code, facts.detail)
    else:
        detail = facts.detail if isinstance(facts, FactsFailure) else "no facts available"
        checks.fail("facts", "E_FACTS_UNAVAILABLE", detail)
    return None


def _bind_customer(inp: EvaluationInput, checks: _Checks, owner: str | None) -> None:
    if not inp.session_customer:
        checks.fail("customer_binding", "E_NO_SESSION_CUSTOMER", "no authenticated customer in this session")
    elif owner != inp.session_customer:
        checks.fail(
            "customer_binding",
            "E_SUBJECT_MISMATCH",
            f"belongs to {owner or 'no customer'}, session customer is {inp.session_customer}",
        )
    else:
        checks.ok("customer_binding", f"belongs to session customer {inp.session_customer}")


# ---------------------------------------------------------------- refunds


def _create_refund(inp: EvaluationInput, checks: _Checks) -> Decimal | None:
    policy = inp.mandate.refunds
    args = inp.arguments

    requested: Decimal | None = None
    amount_given = args.get("amount") is not None
    if not isinstance(args.get("capture_id"), str) or not args["capture_id"]:
        checks.fail("request_valid", "E_INVALID_REQUEST", "capture_id is required")
    elif amount_given:
        amount = args["amount"]
        try:
            if not isinstance(amount, dict):
                raise ValueError("amount must be {currency_code, value}")
            requested = _money(amount.get("value"))
        except ValueError as error:
            checks.fail("request_valid", "E_INVALID_REQUEST", str(error))
        else:
            if amount.get("currency_code") != inp.mandate.currency:
                checks.fail(
                    "currency", "E_CURRENCY_NOT_ALLOWED", f"{amount.get('currency_code')} is not {inp.mandate.currency}"
                )
            else:
                checks.ok("request_valid", f"refund {_usd(requested)} on {args['capture_id']}")
    else:
        checks.ok("request_valid", f"full refund requested on {args['capture_id']} (amount resolved from PayPal)")

    facts: CaptureFacts | None = _facts_check(inp, checks, CaptureFacts, "E_UNKNOWN_CAPTURE")
    if facts is not None:
        _bind_customer(inp, checks, facts.customer_id)
        if facts.currency != inp.mandate.currency:
            checks.fail("currency", "E_CURRENCY_NOT_ALLOWED", f"capture is in {facts.currency}")

    # The amount that will actually move: what the agent asked for, or everything left if it didn't say.
    # An amount that was given but didn't parse stays unknown; it must not become "everything".
    if amount_given:
        amount = requested
    else:
        amount = facts.refundable if facts else None

    if facts is not None and amount is not None:
        if facts.refundable == 0 or amount > facts.refundable:
            checks.fail(
                "within_capture",
                "E_EXCEEDS_CAPTURE",
                f"{_usd(amount)} requested, {_usd(facts.refundable)} left of {_usd(facts.amount)} captured",
            )
        else:
            checks.ok("within_capture", f"{_usd(amount)} of {_usd(facts.refundable)} refundable")

    if amount is None:
        for rule in ("hard_limit", "order_age", "frequency", "daily_total", "auto_approve"):
            checks.skip(rule, "amount unknown")
        return None

    if amount > policy.review_max:
        checks.fail("hard_limit", "E_ABOVE_HARD_LIMIT", f"{_usd(amount)} is above the {_usd(policy.review_max)} limit")
    else:
        checks.ok("hard_limit", f"{_usd(amount)} is within the {_usd(policy.review_max)} limit")

    if policy.max_order_age_days is None:
        checks.ok("order_age", "no refund window set")
    elif facts is None:
        checks.skip("order_age", "capture date unknown")
    else:
        age_days = (inp.now - facts.captured_at).days
        if age_days > policy.max_order_age_days:
            checks.fail("order_age", "E_OUTSIDE_WINDOW", f"captured {age_days} days ago, window is {policy.max_order_age_days}")
        else:
            checks.ok("order_age", f"captured {age_days} days ago, window is {policy.max_order_age_days}")

    if policy.max_refunds_per_customer is None:
        checks.ok("frequency", "no per-customer limit set")
    elif inp.ledger.customer_refunds_in_window >= policy.max_refunds_per_customer:
        checks.fail(
            "frequency",
            "E_FREQUENCY",
            f"{inp.ledger.customer_refunds_in_window} refunds in the last {policy.per_customer_window_days} days"
            f" (limit {policy.max_refunds_per_customer})",
        )
    else:
        checks.ok(
            "frequency",
            f"{inp.ledger.customer_refunds_in_window} of {policy.max_refunds_per_customer} refunds used"
            f" in {policy.per_customer_window_days} days",
        )

    if policy.daily_total_max is None:
        checks.ok("daily_total", "no daily limit set")
    else:
        projected = inp.ledger.refunded_today + amount
        if projected > policy.daily_total_max:
            checks.fail(
                "daily_total",
                "E_DAILY_LIMIT",
                f"{_usd(inp.ledger.refunded_today)} today + {_usd(amount)} > {_usd(policy.daily_total_max)}",
            )
        else:
            checks.ok("daily_total", f"{_usd(projected)} of {_usd(policy.daily_total_max)} today")

    if amount > policy.auto_approve_max:
        checks.review(
            "auto_approve", "R_ABOVE_AUTO_LIMIT", f"{_usd(amount)} is above the {_usd(policy.auto_approve_max)} auto-approve limit"
        )
    else:
        checks.ok("auto_approve", f"{_usd(amount)} is within the {_usd(policy.auto_approve_max)} auto-approve limit")
    return amount


def _get_order(inp: EvaluationInput, checks: _Checks) -> None:
    facts: OrderFacts | None = _facts_check(inp, checks, OrderFacts, "E_UNKNOWN_CAPTURE")
    if facts is not None:
        _bind_customer(inp, checks, facts.customer_id)


def _get_refund(inp: EvaluationInput, checks: _Checks) -> None:
    facts: CaptureFacts | None = _facts_check(inp, checks, CaptureFacts, "E_UNKNOWN_CAPTURE")
    if facts is not None:
        _bind_customer(inp, checks, facts.customer_id)


# ---------------------------------------------------------------- invoices


def _check_recipients(inp: EvaluationInput, checks: _Checks, emails: list[str], rule: str) -> None:
    if not inp.mandate.invoices.known_customers_only:
        checks.ok(rule, "any recipient allowed")
        return
    unknown = [email for email in emails if email.lower() not in inp.known_customer_emails]
    if unknown:
        checks.fail(rule, "E_UNKNOWN_RECIPIENT", f"not existing customers: {', '.join(unknown)}")
    else:
        checks.ok(rule, f"all recipients are existing customers ({len(emails)})")


def _check_additional_recipients(inp: EvaluationInput, checks: _Checks) -> None:
    extra = inp.arguments.get("additional_recipients") or []
    if not extra:
        checks.ok("additional_recipients", "none")
    elif not inp.mandate.invoices.allow_additional_recipients:
        checks.fail("additional_recipients", "E_UNAPPROVED_RECIPIENT", f"extra recipients not allowed: {', '.join(extra)}")
    else:
        _check_recipients(inp, checks, list(extra), "additional_recipients")


def _invoice_upper_bound(args: dict) -> Decimal:
    """Most the invoice can charge: items × quantity plus tax and shipping, ignoring discounts."""
    total = Decimal("0")
    for item in args.get("items") or []:
        quantity = Decimal(str(item["quantity"]))
        unit = _money(item["unit_amount"]["value"])
        if quantity <= 0:
            raise ValueError(f"quantity must be positive: {item['quantity']!r}")
        tax_percent = Decimal(str((item.get("tax") or {}).get("percent", "0")))
        total += quantity * unit * (1 + tax_percent / 100)
    if args.get("shipping_cost"):
        total += Decimal(str(args["shipping_cost"]))
    return total.quantize(CENT)


def _amount_thresholds(inp: EvaluationInput, checks: _Checks, amount: Decimal, review: bool) -> None:
    policy = inp.mandate.invoices
    if amount > policy.review_max:
        checks.fail("hard_limit", "E_ABOVE_HARD_LIMIT", f"{_usd(amount)} is above the {_usd(policy.review_max)} limit")
        return
    checks.ok("hard_limit", f"{_usd(amount)} is within the {_usd(policy.review_max)} limit")
    if not review:
        return
    if amount > policy.auto_approve_max:
        checks.review(
            "auto_approve", "R_ABOVE_AUTO_LIMIT", f"{_usd(amount)} is above the {_usd(policy.auto_approve_max)} auto-approve limit"
        )
    else:
        checks.ok("auto_approve", f"{_usd(amount)} is within the {_usd(policy.auto_approve_max)} auto-approve limit")


def _create_invoice(inp: EvaluationInput, checks: _Checks) -> Decimal | None:
    # Drafting moves no money; the review step happens when the invoice is sent.
    args = inp.arguments
    emails = [
        (recipient.get("billing_info") or {}).get("email_address")
        for recipient in args.get("primary_recipients") or []
    ]
    try:
        if args.get("currency_code") != inp.mandate.currency:
            raise ValueError(f"currency must be {inp.mandate.currency}")
        if not args.get("items"):
            raise ValueError("items are required")
        if not emails or not all(emails):
            raise ValueError("every recipient needs an email address")
        total = _invoice_upper_bound(args)
    except (ValueError, KeyError, TypeError, InvalidOperation) as error:
        checks.fail("request_valid", "E_INVALID_REQUEST", f"invalid invoice: {error}")
        return None
    checks.ok("request_valid", f"invoice up to {_usd(total)} for {len(emails)} recipient(s)")
    _check_recipients(inp, checks, emails, "recipients")
    _amount_thresholds(inp, checks, total, review=False)
    return total


def _send_invoice(inp: EvaluationInput, checks: _Checks) -> Decimal | None:
    facts: InvoiceFacts | None = _facts_check(inp, checks, InvoiceFacts, "E_UNKNOWN_INVOICE")
    _check_additional_recipients(inp, checks)
    if facts is None:
        return None
    _check_recipients(inp, checks, list(facts.recipient_emails), "recipients")
    _amount_thresholds(inp, checks, facts.total, review=True)
    return facts.total


def _send_invoice_reminder(inp: EvaluationInput, checks: _Checks) -> None:
    facts: InvoiceFacts | None = _facts_check(inp, checks, InvoiceFacts, "E_UNKNOWN_INVOICE")
    _check_additional_recipients(inp, checks)
    if facts is not None:
        _check_recipients(inp, checks, list(facts.recipient_emails), "recipients")


_HANDLERS = {
    "create_refund": _create_refund,
    "get_order": _get_order,
    "get_refund": _get_refund,
    "create_invoice": _create_invoice,
    "send_invoice": _send_invoice,
    "send_invoice_reminder": _send_invoice_reminder,
    # get_invoice, list_invoices, cancel_sent_invoice: covered by the gate checks alone.
}


def _decide(checks: _Checks, resolved: Decimal | None) -> Decision:
    failures = [check.code for check in checks.items if check.result is CheckResult.FAIL]
    reviews = [check.code for check in checks.items if check.result is CheckResult.REVIEW]
    if failures:
        outcome, code = Outcome.BLOCK, failures[0]
    elif reviews:
        outcome, code = Outcome.REVIEW, reviews[0]
    else:
        outcome, code = Outcome.ALLOW, None
    return Decision(
        outcome=outcome,
        checks=tuple(checks.items),
        code=code,
        resolved_amount=resolved,
        agent_hint=AGENT_HINTS.get(code, "") if code else "",
        codes=tuple(failures + reviews),
    )
