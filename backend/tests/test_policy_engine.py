from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from intentguard.demo import BILLING_MANDATE, KNOWN_CUSTOMER_EMAILS, SUPPORT_MANDATE
from intentguard.policy.engine import evaluate
from intentguard.policy.models import (
    AgentView,
    CaptureFacts,
    CheckResult,
    EvaluationInput,
    FactsFailure,
    InvoiceFacts,
    LedgerSnapshot,
    OrderFacts,
    Outcome,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

SUPPORT = AgentView("support-agent", True, frozenset({"get_order", "get_refund", "create_refund"}))
BILLING = AgentView(
    "billing-agent", True, frozenset({"create_invoice", "send_invoice", "send_invoice_reminder", "get_invoice"})
)


def capture(customer="cust_ben", amount="42.00", refunded="0", days_ago=5, capture_id="CAP_BEN_COFFEE"):
    return CaptureFacts(
        capture_id=capture_id,
        order_id="ORDER1",
        customer_id=customer,
        amount=Decimal(amount),
        currency="USD",
        already_refunded=Decimal(refunded),
        status="COMPLETED",
        captured_at=NOW - timedelta(days=days_ago),
    )


def refund_args(value=None, capture_id="CAP_BEN_COFFEE", currency="USD"):
    args = {"capture_id": capture_id}
    if value is not None:
        args["amount"] = {"currency_code": currency, "value": value}
    return args


def refund_input(value=None, facts=None, session="cust_ben", ledger=LedgerSnapshot(), mandate=SUPPORT_MANDATE, **kw):
    return EvaluationInput(
        agent=kw.pop("agent", SUPPORT),
        mandate=mandate,
        tool=kw.pop("tool", "create_refund"),
        arguments=kw.pop("arguments", refund_args(value)),
        session_customer=session,
        facts=capture() if facts is None else facts,
        ledger=ledger,
        known_customer_emails=KNOWN_CUSTOMER_EMAILS,
        now=NOW,
    )


def result_of(decision, rule):
    return next(check.result for check in decision.checks if check.rule == rule)


# ---------------------------------------------------------------- the demo scenarios


def test_scenario_a_small_refund_on_own_order_is_allowed():
    decision = evaluate(refund_input("38.00", facts=capture("cust_ben", "42.00")))
    assert decision.outcome is Outcome.ALLOW
    assert decision.resolved_amount == Decimal("38.00")
    assert all(check.result is CheckResult.PASS for check in decision.checks)


def test_scenario_b_injected_900_refund_is_blocked_for_every_reason():
    decision = evaluate(refund_input("900.00"))
    assert decision.outcome is Outcome.BLOCK
    assert decision.code == "E_EXCEEDS_CAPTURE"
    assert {"E_EXCEEDS_CAPTURE", "E_ABOVE_HARD_LIMIT", "E_DAILY_LIMIT"} <= set(decision.codes)
    assert "refund" in decision.agent_hint.lower()


def test_scenario_b_refunding_someone_elses_order_is_blocked():
    carla = capture("cust_carla", "160.00", capture_id="CAP_CARLA")
    decision = evaluate(refund_input("20.00", facts=carla, arguments=refund_args("20.00", "CAP_CARLA")))
    assert decision.outcome is Outcome.BLOCK
    assert decision.code == "E_SUBJECT_MISMATCH"


def test_scenario_c_mid_size_refund_needs_review():
    carla = capture("cust_carla", "160.00")
    decision = evaluate(refund_input("160.00", facts=carla, session="cust_carla"))
    assert decision.outcome is Outcome.REVIEW
    assert decision.code == "R_ABOVE_AUTO_LIMIT"


def test_scenario_d_billing_agent_cannot_refund():
    decision = evaluate(refund_input("20.00", agent=BILLING, mandate=BILLING_MANDATE))
    assert decision.outcome is Outcome.BLOCK
    assert decision.code == "E_TOOL_NOT_ALLOWED"
    # Gate failures stop evaluation: no facts or amount checks ran.
    assert {check.rule for check in decision.checks} == {"agent_active", "mandate_active", "tool_allowed"}


# ---------------------------------------------------------------- amounts come from PayPal, not the agent


def test_omitted_amount_means_full_remaining_and_is_limit_checked():
    planter = capture("cust_dev", "240.00")
    decision = evaluate(refund_input(None, facts=planter, session="cust_dev"))
    assert decision.resolved_amount == Decimal("240.00")
    assert decision.outcome is Outcome.BLOCK
    assert "E_ABOVE_HARD_LIMIT" in decision.codes


def test_omitted_amount_on_small_order_resolves_and_allows():
    decision = evaluate(refund_input(None, facts=capture("cust_ben", "42.00", refunded="2.00")))
    assert decision.outcome is Outcome.ALLOW
    assert decision.resolved_amount == Decimal("40.00")


def test_earlier_refunds_reduce_what_is_refundable():
    decision = evaluate(refund_input("35.00", facts=capture("cust_ben", "42.00", refunded="10.00")))
    assert decision.code == "E_EXCEEDS_CAPTURE"


def test_fully_refunded_capture_blocks_even_without_amount():
    decision = evaluate(refund_input(None, facts=capture(refunded="42.00")))
    assert decision.code == "E_EXCEEDS_CAPTURE"


@pytest.mark.parametrize(
    "amount",
    [{"currency_code": "USD", "value": "-5"}, {"currency_code": "USD", "value": "abc"},
     {"currency_code": "USD", "value": "1.001"}, {"currency_code": "USD", "value": "NaN"},
     {"currency_code": "USD", "value": "0"}, "38.00"],
)
def test_malformed_amount_is_blocked_and_never_becomes_a_full_refund(amount):
    decision = evaluate(refund_input(arguments={"capture_id": "CAP_BEN_COFFEE", "amount": amount}))
    assert decision.outcome is Outcome.BLOCK
    assert decision.code == "E_INVALID_REQUEST"
    assert decision.resolved_amount is None


def test_other_currency_is_blocked():
    decision = evaluate(refund_input(arguments=refund_args("10.00", currency="EUR")))
    assert decision.code == "E_CURRENCY_NOT_ALLOWED"


# ---------------------------------------------------------------- facts fail closed


def test_unknown_capture_is_blocked():
    decision = evaluate(refund_input("10.00", facts=FactsFailure(not_found=True, detail="404")))
    assert decision.code == "E_UNKNOWN_CAPTURE"


def test_paypal_unreachable_blocks():
    decision = evaluate(refund_input("10.00", facts=FactsFailure(not_found=False, detail="timeout")))
    assert decision.outcome is Outcome.BLOCK
    assert decision.code == "E_FACTS_UNAVAILABLE"


def test_missing_facts_blocks():
    decision = evaluate(replace(refund_input("10.00"), facts=None))
    assert decision.code == "E_FACTS_UNAVAILABLE"


def test_no_session_customer_blocks():
    decision = evaluate(refund_input("10.00", session=None))
    assert decision.code == "E_NO_SESSION_CUSTOMER"


# ---------------------------------------------------------------- mandate limits


def test_order_outside_window_is_blocked():
    decision = evaluate(refund_input("10.00", facts=capture(days_ago=45)))
    assert decision.code == "E_OUTSIDE_WINDOW"


def test_second_refund_for_customer_in_window_is_blocked():
    decision = evaluate(refund_input("10.00", ledger=LedgerSnapshot(customer_refunds_in_window=1)))
    assert decision.code == "E_FREQUENCY"


def test_daily_total_includes_today_and_this_refund():
    assert evaluate(refund_input("20.00", ledger=LedgerSnapshot(refunded_today=Decimal("280")))).outcome is Outcome.ALLOW
    blocked = evaluate(refund_input("20.01", ledger=LedgerSnapshot(refunded_today=Decimal("280"))))
    assert blocked.code == "E_DAILY_LIMIT"


@pytest.mark.parametrize(
    "mandate",
    [None, SUPPORT_MANDATE.model_copy(update={"status": "REVOKED"}),
     SUPPORT_MANDATE.model_copy(update={"expires_at": NOW}),
     SUPPORT_MANDATE.model_copy(update={"valid_from": NOW + timedelta(days=1)}),
     BILLING_MANDATE],
)
def test_inactive_or_foreign_mandate_blocks(mandate):
    decision = evaluate(refund_input("10.00", mandate=mandate))
    assert decision.code == "E_MANDATE_INACTIVE"


def test_disabled_agent_blocks():
    decision = evaluate(refund_input("10.00", agent=replace(SUPPORT, active=False)))
    assert decision.code == "E_AGENT_INACTIVE"


def test_mandate_without_refund_section_blocks_refunds():
    decision = evaluate(refund_input("10.00", mandate=SUPPORT_MANDATE.model_copy(update={"refunds": None})))
    assert decision.code == "E_NOT_IN_MANDATE"


# ---------------------------------------------------------------- reads are customer-bound too


def test_get_order_of_another_customer_is_blocked():
    decision = evaluate(
        refund_input(tool="get_order", arguments={"id": "ORDER1"}, facts=OrderFacts("ORDER1", "cust_carla"))
    )
    assert decision.code == "E_SUBJECT_MISMATCH"


def test_get_order_of_own_order_is_allowed():
    decision = evaluate(refund_input(tool="get_order", arguments={"id": "ORDER1"}, facts=OrderFacts("ORDER1", "cust_ben")))
    assert decision.outcome is Outcome.ALLOW


# ---------------------------------------------------------------- invoices


def invoice_input(tool, arguments, facts=None):
    return EvaluationInput(
        agent=BILLING,
        mandate=BILLING_MANDATE,
        tool=tool,
        arguments=arguments,
        session_customer=None,
        facts=facts,
        ledger=LedgerSnapshot(),
        known_customer_emails=KNOWN_CUSTOMER_EMAILS,
        now=NOW,
    )


def draft(email="ana.lima@example.com", unit="400.00", quantity="2", tax=None, shipping=None):
    item = {"name": "Custom tiles", "quantity": quantity, "unit_amount": {"currency_code": "USD", "value": unit}}
    if tax:
        item["tax"] = {"name": "Sales tax", "percent": tax}
    args = {"currency_code": "USD", "primary_recipients": [{"billing_info": {"email_address": email}}], "items": [item]}
    if shipping:
        args["shipping_cost"] = shipping
    return args


def invoice_facts(total="1500.00", emails=("ana.lima@example.com",)):
    return InvoiceFacts("INV2-1", "DRAFT", tuple(emails), Decimal(total), "USD")


def test_create_invoice_for_known_customer_is_allowed_even_above_auto_limit():
    decision = evaluate(invoice_input("create_invoice", draft(unit="700.00")))
    assert decision.outcome is Outcome.ALLOW
    assert decision.resolved_amount == Decimal("1400.00")


def test_create_invoice_total_includes_tax_and_shipping():
    decision = evaluate(invoice_input("create_invoice", draft(unit="900.00", quantity="2", tax="10", shipping="25")))
    assert decision.resolved_amount == Decimal("2005.00")
    assert decision.code == "E_ABOVE_HARD_LIMIT"


def test_create_invoice_to_stranger_is_blocked():
    decision = evaluate(invoice_input("create_invoice", draft(email="attacker@evil.example")))
    assert decision.code == "E_UNKNOWN_RECIPIENT"


def test_create_invoice_without_items_is_invalid():
    args = draft()
    args["items"] = []
    assert evaluate(invoice_input("create_invoice", args)).code == "E_INVALID_REQUEST"


def test_send_invoice_above_auto_limit_needs_review():
    decision = evaluate(invoice_input("send_invoice", {"invoice_id": "INV2-1"}, invoice_facts("1500.00")))
    assert decision.outcome is Outcome.REVIEW


def test_send_invoice_with_extra_recipient_is_blocked():
    args = {"invoice_id": "INV2-1", "additional_recipients": ["attacker@evil.example"]}
    decision = evaluate(invoice_input("send_invoice", args, invoice_facts("100.00")))
    assert decision.code == "E_UNAPPROVED_RECIPIENT"


def test_send_invoice_created_outside_for_stranger_is_blocked():
    decision = evaluate(invoice_input("send_invoice", {"invoice_id": "INV2-1"}, invoice_facts("100.00", ("x@y.example",))))
    assert decision.code == "E_UNKNOWN_RECIPIENT"


def test_reminder_with_extra_recipient_is_blocked():
    args = {"invoice_id": "INV2-1", "additional_recipients": ["attacker@evil.example"]}
    decision = evaluate(invoice_input("send_invoice_reminder", args, invoice_facts()))
    assert decision.code == "E_UNAPPROVED_RECIPIENT"


def test_billing_mandate_does_not_cover_order_lookups():
    agent = replace(BILLING, allowed_tools=BILLING.allowed_tools | {"get_order"})
    decision = evaluate(replace(invoice_input("get_order", {"id": "ORDER1"}, OrderFacts("ORDER1", None)), agent=agent))
    assert decision.code == "E_NOT_IN_MANDATE"


# ---------------------------------------------------------------- properties


def test_same_input_same_decision():
    inp = refund_input("38.00")
    assert evaluate(inp) == evaluate(inp)


def cents(max_dollars):
    return st.integers(min_value=1, max_value=max_dollars * 100).map(lambda c: Decimal(c) / 100)


# Weighted toward realistic values so ALLOW and REVIEW are common, not just BLOCK.
@settings(max_examples=400, deadline=None)
@given(
    requested=st.one_of(st.none(), cents(250), cents(2000)),
    captured=st.one_of(cents(300), cents(2000)),
    refunded_fraction=st.sampled_from([0, 0, 0, 25, 50, 100]),
    refunded_today=st.one_of(st.just(Decimal("0")), cents(350)),
    prior_refunds=st.sampled_from([0, 0, 0, 1, 2]),
    owner=st.sampled_from(["cust_ben", "cust_ben", "cust_ben", "cust_carla", None]),
    days_ago=st.integers(min_value=0, max_value=45),
)
def test_refund_invariants(requested, captured, refunded_fraction, refunded_today, prior_refunds, owner, days_ago):
    refunded = (captured * refunded_fraction / 100).quantize(Decimal("0.01"))
    facts = capture(owner, str(captured), str(refunded), days_ago)
    ledger = LedgerSnapshot(customer_refunds_in_window=prior_refunds, refunded_today=refunded_today)
    decision = evaluate(refund_input(None if requested is None else str(requested), facts=facts, ledger=ledger))
    policy = SUPPORT_MANDATE.refunds

    if decision.outcome is not Outcome.BLOCK:
        amount = decision.resolved_amount
        assert amount is not None and amount > 0
        assert owner == "cust_ben"
        assert amount <= captured - refunded
        assert amount <= policy.review_max
        assert refunded_today + amount <= policy.daily_total_max
        assert prior_refunds < policy.max_refunds_per_customer
        assert days_ago <= policy.max_order_age_days
    if decision.outcome is Outcome.ALLOW:
        assert decision.resolved_amount <= policy.auto_approve_max
    if decision.outcome is Outcome.BLOCK:
        assert decision.code and decision.code.startswith("E_")
