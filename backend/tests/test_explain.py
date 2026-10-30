from intentguard.gateway.explain import explain


def row(**fields):
    base = {"agent_id": "support-agent", "tool": "create_refund", "amount": "900.00", "customer_name": "Ben Okafor",
            "decision": "BLOCK", "codes": [], "status": "STOPPED", "owner_approved": False}
    return base | fields


def test_block_lists_every_reason_in_plain_words():
    sentence = explain(row(codes=["E_EXCEEDS_CAPTURE", "E_ABOVE_HARD_LIMIT", "E_DAILY_LIMIT", "R_ABOVE_AUTO_LIMIT"]), [])
    assert sentence == (
        "Stopped before PayPal: the Support Agent tried to refund $900.00 to Ben Okafor, but that's more than is left "
        "to refund on the payment, it's above your hard limit and it would go over today's refund cap."
    )


def test_review_uses_the_rule_that_held_it_and_the_owners_outcome():
    checks = [{"rule": "auto_approve", "result": "REVIEW", "detail": "$160.00 is above the $50.00 auto-approve limit"}]
    held = explain(row(decision="REVIEW", amount="160.00", customer_name="Carla Diaz", status="AWAITING_OWNER"), checks)
    assert held.startswith("Waiting for you: the Support Agent asked to refund $160.00 to Carla Diaz")
    assert held.endswith("$160.00 is above the $50.00 auto-approve limit.")
    assert explain(row(decision="REVIEW", status="REJECTED"), checks).startswith("You rejected it")


def test_allowed_refunds_say_what_paypal_did():
    assert explain(row(decision="ALLOW", amount="38.00", customer_name="Ana Lima", status="COMPLETED"), []) == \
        "Done within your rules: PayPal refund $38.00 to Ana Lima was completed."
    approved = explain(row(decision="ALLOW", status="COMPLETED", owner_approved=True), [])
    assert approved.startswith("Done after your approval")
    assert "rejected" in explain(row(decision="ALLOW", status="PAYPAL_ERROR"), [])
    assert "reconciler" in explain(row(decision="ALLOW", status="UNKNOWN"), [])


def test_other_tools_and_unknown_codes_still_read_as_sentences():
    sentence = explain(row(agent_id="billing-agent", tool="send_invoice", amount="1500", codes=["E_SOMETHING_NEW"]), [])
    assert sentence == "Stopped before PayPal: the Billing Agent tried to send a $1,500.00 invoice, but E_SOMETHING_NEW."
