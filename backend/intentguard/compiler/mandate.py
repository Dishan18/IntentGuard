"""The Mandate Compiler: the owner's plain-English rules → a typed, checkable mandate.

The LLM only drafts. Deterministic code decides whether the draft can be activated:

1. **Structure:** Gemini answers in JSON constrained to the policy schema, then Pydantic validates
   it (types, limits in the right order). Invalid output is never stored.
2. **Grounding:** every number in the draft must appear in the owner's text ("$50", "30 days",
   "one refund", "per week"). A limit the model made up blocks activation.
3. **Honesty about gaps:** anything the model couldn't express ("never accept a dispute over $100")
   is listed as not enforced, and capability mismatches (refund rules for an agent that can't
   refund) are flagged.
4. **Examples:** the draft is run through the real policy engine on a handful of situations built
   from its own thresholds, so the owner sees what it will actually do before activating.

Activation is a separate, explicit owner action (gateway/mandates.py); compiling never changes
what the gateway enforces.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError

from intentguard.agents.llm import GeminiClient
from intentguard.gateway.registry import Agent
from intentguard.policy.engine import evaluate
from intentguard.policy.models import (
    AgentView,
    CaptureFacts,
    CheckResult,
    EvaluationInput,
    InvoiceFacts,
    InvoicePolicy,
    LedgerSnapshot,
    Mandate,
    RefundPolicy,
)

REFUND_TOOLS = {"create_refund"}
INVOICE_TOOLS = {"create_invoice", "send_invoice", "send_invoice_reminder"}

_nullable = lambda schema: {"anyOf": [schema, {"type": "null"}]}  # noqa: E731

POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "refunds": _nullable({
            "type": "object",
            "properties": {
                "auto_approve_max": {"type": "number", "description": "Refunds up to this amount run without the owner."},
                "review_max": {"type": "number", "description": "Above auto_approve_max and up to this: owner approval. Above: blocked."},
                "max_order_age_days": _nullable({"type": "integer"}),
                "max_refunds_per_customer": _nullable({"type": "integer"}),
                "per_customer_window_days": {"type": "integer"},
                "daily_total_max": _nullable({"type": "number"}),
            },
            "required": ["auto_approve_max", "review_max", "max_order_age_days", "max_refunds_per_customer",
                         "per_customer_window_days", "daily_total_max"],
        }),
        "invoices": _nullable({
            "type": "object",
            "properties": {
                "auto_approve_max": {"type": "number", "description": "Invoices up to this total are sent without the owner."},
                "review_max": {"type": "number", "description": "Above auto_approve_max and up to this: owner approval. Above: blocked."},
                "known_customers_only": {"type": "boolean"},
                "allow_additional_recipients": {"type": "boolean"},
            },
            "required": ["auto_approve_max", "review_max", "known_customers_only", "allow_additional_recipients"],
        }),
        "mapping": {
            "type": "array",
            "description": "Each sentence or clause of the owner's text and the field it became.",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "becomes": {"type": "string"}},
                "required": ["text", "becomes"],
            },
        },
        "unsupported": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Quoted parts of the text that no field can express.",
        },
    },
    "required": ["refunds", "invoices", "mapping", "unsupported"],
}

SYSTEM_PROMPT = """\
You translate a small merchant's plain-English instructions for one AI agent into a policy for a
payment gateway. Output only the JSON the schema asks for.

Fields:
- refunds: null if the text forbids refunds or never mentions them.
  - auto_approve_max: refunds up to this amount run automatically.
  - review_max: refunds above auto_approve_max and up to review_max wait for the owner's approval;
    anything above review_max is blocked. If the text has no approval band, set review_max equal to
    auto_approve_max.
  - max_order_age_days: only orders younger than this many days, or null.
  - max_refunds_per_customer and per_customer_window_days: e.g. "one refund per customer per 30 days"
    → 1 and 30; "per month" → 30, "per week" → 7. If no limit: null and 30.
  - daily_total_max: cap on the total refunded per day, or null.
- invoices: null if the text never mentions invoices.
  - auto_approve_max / review_max: as for refunds, on the invoice total.
  - known_customers_only: true if invoices may only go to existing customers.
  - allow_additional_recipients: true only if the text allows extra or CC recipients.

Rules:
- Use only numbers the text states (words like "one" count). Never invent a limit.
- "Never refund more than the order total" is always enforced by the gateway: map it to "built in".
- Anything no field can express (disputes, time of day, product types...) goes in `unsupported`,
  quoted from the text. Do not stretch a field to cover it.
- Ignore words that aren't instructions (greetings, reasons, labels like "Holiday rush").
- In `mapping`, list each clause of the text and which field it became ("refunds.daily_total_max",
  "built in", or "unsupported").
"""

_WORD_NUMBERS = {
    "one": 1, "once": 1, "a single": 1, "two": 2, "twice": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fourteen": 14, "thirty": 30,
}
_PERIODS = {"day": 1, "daily": 1, "week": 7, "weekly": 7, "month": 30, "monthly": 30, "year": 365, "yearly": 365}


def stated_numbers(text: str) -> set[Decimal]:
    """Every number the owner wrote, including "one", "twice" and "per month" (30 days)."""
    lowered = text.lower()
    numbers = {Decimal(match.replace(",", "")) for match in re.findall(r"\d[\d,]*(?:\.\d+)?", lowered)}
    for word, value in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", lowered):
            numbers.add(Decimal(value))
    for word, value in _PERIODS.items():
        if re.search(rf"\b{word}\b", lowered):
            numbers.add(Decimal(value))
    if re.search(r"\b(?:a|an|per|each|every)\s+(?:refund|invoice)\b", lowered):
        numbers.add(Decimal(1))
    return numbers


def _usd(value) -> str:
    return f"${Decimal(value):,.2f}"


def render_rules(mandate: Mandate) -> list[str]:
    """The mandate as sentences the owner can check, generated from the typed fields."""
    rules = []
    refunds = mandate.refunds
    if refunds is None:
        rules.append("No refunds: every refund attempt is blocked.")
    else:
        rules.append(f"Refunds up to {_usd(refunds.auto_approve_max)} go through automatically.")
        if refunds.review_max > refunds.auto_approve_max:
            rules.append(
                f"Refunds above {_usd(refunds.auto_approve_max)} and up to {_usd(refunds.review_max)} wait for your approval."
            )
        rules.append(f"Refunds above {_usd(refunds.review_max)} are blocked.")
        if refunds.max_order_age_days is not None:
            rules.append(f"Only orders from the last {refunds.max_order_age_days} days can be refunded.")
        if refunds.max_refunds_per_customer is not None:
            times = "1 refund" if refunds.max_refunds_per_customer == 1 else f"{refunds.max_refunds_per_customer} refunds"
            rules.append(f"At most {times} per customer every {refunds.per_customer_window_days} days.")
        if refunds.daily_total_max is not None:
            rules.append(f"At most {_usd(refunds.daily_total_max)} refunded in total per day.")
        rules.append("Never more than what's left to refund on the payment, and only for the customer in the conversation (always on).")
    invoices = mandate.invoices
    if invoices is not None:
        rules.append(f"Invoices up to {_usd(invoices.auto_approve_max)} are sent automatically.")
        if invoices.review_max > invoices.auto_approve_max:
            rules.append(
                f"Invoices above {_usd(invoices.auto_approve_max)} and up to {_usd(invoices.review_max)} wait for your approval."
            )
        rules.append(f"Invoices above {_usd(invoices.review_max)} are blocked.")
        rules.append(
            "Invoices only to existing customers." if invoices.known_customers_only else "Invoices may go to anyone."
        )
        if not invoices.allow_additional_recipients:
            rules.append("No extra (CC) recipients on invoices.")
    return rules


def _numbers(mandate: Mandate) -> list[tuple[str, Decimal]]:
    found = []
    for section in ("refunds", "invoices"):
        policy = getattr(mandate, section)
        if policy is None:
            continue
        for name, value in policy.model_dump().items():
            if isinstance(value, bool) or value is None:
                continue
            found.append((f"{section}.{name}", Decimal(value)))
    return found


def _grounding_problems(mandate: Mandate, text: str) -> list[str]:
    stated = stated_numbers(text)
    problems = []
    for name, value in _numbers(mandate):
        if value in stated:
            continue
        policy = getattr(mandate, name.split(".")[0])
        # Derived values that need no number of their own.
        if name.endswith("review_max") and value == policy.auto_approve_max:
            continue  # no approval band
        if name == "refunds.per_customer_window_days" and mandate.refunds.max_refunds_per_customer is None:
            continue  # unused default
        problems.append(f"{name} is {value.normalize():f}, a number your text doesn't state")
    return problems


def _capability_warnings(mandate: Mandate, agent: Agent | None) -> list[str]:
    if agent is None:
        return ["this agent isn't registered, so it can't call anything"]
    warnings = []
    if mandate.refunds is not None and not (agent.allowed_tools & REFUND_TOOLS):
        warnings.append("refund rules have no effect: this agent isn't allowed to call refund tools")
    if mandate.invoices is not None and not (agent.allowed_tools & INVOICE_TOOLS):
        warnings.append("invoice rules have no effect: this agent isn't allowed to call invoice tools")
    if mandate.refunds is not None and mandate.refunds.daily_total_max is None:
        warnings.append("no daily cap on refunds: the total an agent can refund per day is unlimited")
    return warnings


# ---------------------------------------------------------------- example decisions

_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_CUSTOMER = "cust_example"
_EMAIL = "customer@example.com"


def _capture(amount: Decimal, age_days: int = 3, customer: str = _CUSTOMER) -> CaptureFacts:
    return CaptureFacts(
        capture_id="EXAMPLECAPTURE00001", order_id="EXAMPLEORDER", customer_id=customer, amount=amount,
        currency="USD", already_refunded=Decimal("0"), status="COMPLETED", captured_at=_NOW - timedelta(days=age_days),
    )


def _run(mandate: Mandate, agent: Agent | None, tool: str, arguments: dict, facts, ledger=LedgerSnapshot()) -> dict:
    view = AgentView(agent.id, True, agent.allowed_tools) if agent else AgentView("unknown", False, frozenset())
    decision = evaluate(EvaluationInput(
        agent=view, mandate=mandate, tool=tool, arguments=arguments, session_customer=_CUSTOMER, facts=facts,
        ledger=ledger, known_customer_emails=frozenset({_EMAIL}), now=_NOW,
    ))
    deciding = [c for c in decision.checks if c.result is CheckResult.FAIL] or [
        c for c in decision.checks if c.result is CheckResult.REVIEW
    ]
    return {
        "decision": decision.outcome.value,
        "codes": list(decision.codes),
        "why": deciding[0].detail if deciding else "every rule passes",
    }


def _refund(amount: Decimal) -> dict:
    return {"capture_id": "EXAMPLECAPTURE00001", "amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}


def _invoice_facts(total: Decimal, email: str = _EMAIL) -> InvoiceFacts:
    return InvoiceFacts(invoice_id="INV2-EXAMPLE", status="DRAFT", recipient_emails=(email,), total=total, currency="USD")


def example_decisions(mandate: Mandate, agent: Agent | None) -> list[dict]:
    """What this mandate would decide in situations built from its own thresholds."""
    examples = []

    def add(scenario: str, tool: str, arguments: dict, facts, ledger=LedgerSnapshot()):
        examples.append({"scenario": scenario, **_run(mandate, agent, tool, arguments, facts, ledger)})

    refunds = mandate.refunds
    if refunds is None:
        add("A $20.00 refund on a recent order", "create_refund", _refund(Decimal("20")), _capture(Decimal("20")))
    else:
        small = min(refunds.auto_approve_max, Decimal("20")) or Decimal("10")
        add(f"A {_usd(small)} refund on a recent order", "create_refund", _refund(small), _capture(small))
        if refunds.review_max > refunds.auto_approve_max:
            middle = ((refunds.auto_approve_max + refunds.review_max) / 2).quantize(Decimal("1"))
            add(f"A {_usd(middle)} refund", "create_refund", _refund(middle), _capture(middle))
        big = refunds.review_max + 100
        add(f"A {_usd(big)} refund", "create_refund", _refund(big), _capture(big))
        add(f"{_usd(60)} back on a {_usd(40)} order", "create_refund", _refund(Decimal("60")), _capture(Decimal("40")))
        add("A refund on another customer's order", "create_refund", _refund(small), _capture(small, customer="cust_someone_else"))
        if refunds.max_order_age_days is not None:
            old = refunds.max_order_age_days + 5
            add(f"A {_usd(small)} refund on a {old}-day-old order", "create_refund", _refund(small), _capture(small, old))
        if refunds.max_refunds_per_customer is not None:
            add(
                f"A customer who already had {refunds.max_refunds_per_customer} refund(s) asks again",
                "create_refund", _refund(small), _capture(small),
                LedgerSnapshot(customer_refunds_in_window=refunds.max_refunds_per_customer),
            )
        if refunds.daily_total_max is not None:
            add(
                f"A {_usd(small)} refund after {_usd(refunds.daily_total_max)} was already refunded today",
                "create_refund", _refund(small), _capture(small),
                LedgerSnapshot(refunded_today=refunds.daily_total_max),
            )

    invoices = mandate.invoices
    if invoices is not None:
        send = {"invoice_id": "INV2-EXAMPLE"}
        low = (invoices.auto_approve_max / 2).quantize(Decimal("1")) or Decimal("10")
        add(f"Send a {_usd(low)} invoice to an existing customer", "send_invoice", send, _invoice_facts(low))
        if invoices.review_max > invoices.auto_approve_max:
            middle = ((invoices.auto_approve_max + invoices.review_max) / 2).quantize(Decimal("1"))
            add(f"Send a {_usd(middle)} invoice", "send_invoice", send, _invoice_facts(middle))
        big = invoices.review_max + 100
        add(f"Send a {_usd(big)} invoice", "send_invoice", send, _invoice_facts(big))
        if invoices.known_customers_only:
            add("Send an invoice to someone who isn't a customer", "send_invoice", send,
                _invoice_facts(low, "stranger@example.net"))
    return examples


# ---------------------------------------------------------------- compile


@dataclass
class Compiled:
    mandate: Mandate | None
    model: str
    rules: list[str] = field(default_factory=list)
    mapping: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)  # activation is refused while any remain
    examples: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)

    def notes(self) -> dict:
        data = asdict(self)
        data.pop("mandate")
        return data


def changes_between(current: Mandate | None, draft: Mandate) -> list[dict]:
    before = dict(_numbers(current)) if current else {}
    after = dict(_numbers(draft))
    changes = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            changes.append({
                "field": name,
                "from": None if before.get(name) is None else f"{before[name].normalize():f}",
                "to": None if after.get(name) is None else f"{after[name].normalize():f}",
            })
    if current:
        for section in ("refunds", "invoices"):
            if (getattr(current, section) is None) != (getattr(draft, section) is None):
                changes.append({"field": section, "from": "on" if getattr(current, section) else "off",
                                "to": "on" if getattr(draft, section) else "off"})
    return changes


def build(
    data: dict, text: str, agent_id: str, mandate_id: str, agent: Agent | None, current: Mandate | None, model: str
) -> Compiled:
    """Validate and check the model's JSON. Pure: no LLM call, so every check is testable."""
    unsupported = [str(item) for item in data.get("unsupported") or []]
    mapping = [m for m in data.get("mapping") or [] if isinstance(m, dict)]
    try:
        mandate = Mandate(
            id=mandate_id,
            agent_id=agent_id,
            currency=current.currency if current else "USD",
            source_text=text.strip(),
            refunds=RefundPolicy.model_validate(data["refunds"]) if data.get("refunds") else None,
            invoices=InvoicePolicy.model_validate(data["invoices"]) if data.get("invoices") else None,
        )
    except (ValidationError, KeyError, TypeError) as error:
        detail = error.errors()[0]["msg"] if isinstance(error, ValidationError) else str(error)
        return Compiled(None, model, mapping=mapping, unsupported=unsupported,
                        blocking=[f"the draft isn't a valid policy: {detail}"])

    blocking = _grounding_problems(mandate, text)
    if mandate.refunds is None and mandate.invoices is None:
        blocking.append("the draft allows nothing; describe what the agent may do")
    return Compiled(
        mandate=mandate,
        model=model,
        rules=render_rules(mandate),
        mapping=mapping,
        unsupported=unsupported,
        warnings=_capability_warnings(mandate, agent)
        + [f"not enforced (no rule can express it): {item}" for item in unsupported],
        blocking=blocking,
        examples=example_decisions(mandate, agent),
        changes=changes_between(current, mandate),
    )


async def compile_mandate(
    llm: GeminiClient, text: str, agent_id: str, mandate_id: str, agent: Agent | None, current: Mandate | None
) -> Compiled:
    tools = ", ".join(sorted(agent.allowed_tools)) if agent else "none"
    prompt = f"Agent: {agent_id} (PayPal tools it can call: {tools})\n\nThe owner's instructions:\n{text.strip()}"
    generation = await llm.generate(
        SYSTEM_PROMPT, [{"role": "user", "parts": [{"text": prompt}]}], [], response_schema=POLICY_SCHEMA
    )
    raw = "".join(part.get("text", "") for part in generation.content["parts"] if not part.get("thought"))
    try:
        data = json.loads(raw)
    except ValueError:
        return Compiled(None, generation.model, blocking=["the model didn't return valid JSON; try again"])
    return build(data, text, agent_id, mandate_id, agent, current, generation.model)
