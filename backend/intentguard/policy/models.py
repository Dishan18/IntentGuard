"""Inputs and outputs of the policy engine.

A Mandate is what the owner authorized (later compiled from plain English, then confirmed by a
human). Facts are what PayPal and the store say is true. The LedgerSnapshot is IntentGuard's own
running totals. None of these come from the agent: the agent only supplies tool arguments.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------- mandate


class RefundPolicy(BaseModel):
    auto_approve_max: Decimal = Field(ge=0, description="Refunds up to this amount run without review.")
    review_max: Decimal = Field(ge=0, description="Above auto_approve_max and up to this: owner review. Above: blocked.")
    max_order_age_days: int | None = Field(default=None, ge=0)
    max_refunds_per_customer: int | None = Field(default=None, ge=1)
    per_customer_window_days: int = Field(default=30, ge=1)
    daily_total_max: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ordered(self):
        if self.review_max < self.auto_approve_max:
            raise ValueError("review_max must be >= auto_approve_max")
        return self


class InvoicePolicy(BaseModel):
    auto_approve_max: Decimal = Field(ge=0)
    review_max: Decimal = Field(ge=0)
    known_customers_only: bool = True
    allow_additional_recipients: bool = False

    @model_validator(mode="after")
    def _ordered(self):
        if self.review_max < self.auto_approve_max:
            raise ValueError("review_max must be >= auto_approve_max")
        return self


class Mandate(BaseModel):
    id: str
    version: int = 1
    agent_id: str
    status: Literal["ACTIVE", "PAUSED", "REVOKED"] = "ACTIVE"
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    currency: str = "USD"
    refunds: RefundPolicy | None = None
    invoices: InvoicePolicy | None = None
    source_text: str = ""


# ---------------------------------------------------------------- facts


@dataclass(frozen=True)
class CaptureFacts:
    """A captured payment as PayPal reports it."""

    capture_id: str
    order_id: str | None
    customer_id: str | None  # custom_id the store attached at checkout
    amount: Decimal
    currency: str
    already_refunded: Decimal  # COMPLETED + PENDING refunds on this capture
    status: str
    captured_at: datetime

    @property
    def refundable(self) -> Decimal:
        return max(self.amount - self.already_refunded, Decimal("0"))


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    customer_id: str | None


@dataclass(frozen=True)
class InvoiceFacts:
    invoice_id: str
    status: str
    recipient_emails: tuple[str, ...]
    total: Decimal
    currency: str


@dataclass(frozen=True)
class FactsFailure:
    """Facts could not be established. not_found means PayPal says the object doesn't exist."""

    not_found: bool
    detail: str


Facts = CaptureFacts | OrderFacts | InvoiceFacts | FactsFailure | None


@dataclass(frozen=True)
class LedgerSnapshot:
    """IntentGuard's own totals, including amounts reserved by in-flight approved calls."""

    customer_refunds_in_window: int = 0
    refunded_today: Decimal = Decimal("0")


@dataclass(frozen=True)
class AgentView:
    id: str
    active: bool
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class EvaluationInput:
    agent: AgentView
    mandate: Mandate | None
    tool: str
    arguments: dict
    session_customer: str | None
    facts: Facts
    ledger: LedgerSnapshot
    known_customer_emails: frozenset[str]
    now: datetime


# ---------------------------------------------------------------- decision


class Outcome(StrEnum):
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class CheckResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"  # blocks
    REVIEW = "REVIEW"  # needs the owner
    SKIP = "SKIP"  # couldn't run because an earlier check failed


@dataclass(frozen=True)
class Check:
    rule: str
    result: CheckResult
    detail: str
    code: str | None = None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    checks: tuple[Check, ...]
    code: str | None = None  # first blocking code, or the first review code
    resolved_amount: Decimal | None = None  # the amount that will actually move, if any
    agent_hint: str = ""
    codes: tuple[str, ...] = field(default=())
