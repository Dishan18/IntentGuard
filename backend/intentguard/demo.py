"""Demo merchant data: the store's customers and the owner's mandates for the two demo agents.

Customer IDs match the custom_id that scripts/seed_orders.py attaches to each sandbox order.
"""

from decimal import Decimal

from intentguard.policy.models import InvoicePolicy, Mandate, RefundPolicy

CUSTOMERS = {
    "cust_ana": {"name": "Ana Lima", "email": "ana.lima@example.com"},
    "cust_ben": {"name": "Ben Okafor", "email": "ben.okafor@example.com"},
    "cust_carla": {"name": "Carla Diaz", "email": "carla.diaz@example.com"},
    "cust_dev": {"name": "Dev Patel", "email": "dev.patel@example.com"},
}

KNOWN_CUSTOMER_EMAILS = frozenset(customer["email"] for customer in CUSTOMERS.values())

SUPPORT_MANDATE = Mandate(
    id="mnd_support",
    agent_id="support-agent",
    source_text=(
        "Refund up to $50 automatically if the order is under 30 days old. One refund per customer per "
        "30 days. Anything from $50 to $200 needs my approval. Never refund more than the order total, "
        "never more than $300 a day in total."
    ),
    refunds=RefundPolicy(
        auto_approve_max=Decimal("50"),
        review_max=Decimal("200"),
        max_order_age_days=30,
        max_refunds_per_customer=1,
        per_customer_window_days=30,
        daily_total_max=Decimal("300"),
    ),
)

BILLING_MANDATE = Mandate(
    id="mnd_billing",
    agent_id="billing-agent",
    source_text=(
        "Invoices only to existing customers, max $2,000 each, anything above $1,000 needs approval. "
        "It can never issue refunds."
    ),
    invoices=InvoicePolicy(auto_approve_max=Decimal("1000"), review_max=Decimal("2000")),
)

MANDATES = {mandate.agent_id: mandate for mandate in (SUPPORT_MANDATE, BILLING_MANDATE)}
