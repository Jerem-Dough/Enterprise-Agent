"""Approval routing, the backup rule, and who is allowed to say yes.

The brief's rule has two conditions joined by *and*: unanswered at end of day,
**and** the approver is out the next day. Both halves are tested for firing and
for not firing, because a routing rule that triggers on one condition is a rule
that hands somebody else's authority away for no reason.
"""
from __future__ import annotations

import pytest
from conftest import to_approval


def _pending(harness):
    to_approval(harness)
    return harness.approvals.pending()[0]


# -- initial routing ------------------------------------------------------


def test_the_request_starts_with_the_person_whose_agent_it_is(harness):
    record = _pending(harness)
    assert record["requested_of"] == "u-101"
    assert record["original_approver"] == "u-101"
    assert record["routed_reason"] is None


def test_the_deadline_is_end_of_day_from_policy(harness):
    record = _pending(harness)
    assert record["deadline"] == "2026-09-02T17:00:00"


# -- the backup rule ------------------------------------------------------


def test_it_reroutes_when_unanswered_and_the_approver_is_out_tomorrow(harness):
    record = _pending(harness)
    harness.clock.advance_to("2026-09-02T17:00:00")

    moved = harness.approvals.reroute_stale()
    assert len(moved) == 1
    assert moved[0]["requested_of"] == "u-102"
    assert "did not answer by end of day" in moved[0]["routed_reason"]
    assert harness.approvals.get(record["id"])["original_approver"] == "u-101"


def test_it_does_not_reroute_before_end_of_day(harness):
    _pending(harness)
    harness.clock.advance_to("2026-09-02T16:59:00")
    assert harness.approvals.reroute_stale() == []


def test_it_does_not_reroute_when_the_approver_is_in_tomorrow(harness):
    """Remove the out of office and the deadline alone must not be enough."""
    harness.store.conn.execute("delete from calendar_events where id = 'E-002'")
    _pending(harness)
    harness.clock.advance_to("2026-09-02T17:00:00")
    assert harness.approvals.reroute_stale() == []


def test_it_does_not_reroute_something_already_decided(harness):
    record = _pending(harness)
    harness.approvals.decide(record["id"], verdict="approved", by="u-101")
    harness.clock.advance_to("2026-09-02T17:00:00")
    assert harness.approvals.reroute_stale() == []


def test_rerouting_is_not_repeated_on_a_later_tick(harness):
    _pending(harness)
    harness.clock.advance_to("2026-09-02T17:00:00")
    assert len(harness.approvals.reroute_stale()) == 1

    harness.clock.advance_to("2026-09-02T18:00:00")
    second = harness.approvals.reroute_stale()
    # Marcus is not out tomorrow, so it stays with him rather than bouncing on.
    assert second == []


# -- authority does not transfer ------------------------------------------


def _reprice(harness, new_price: float) -> None:
    """Make the order more valuable without tripping the premium rule.

    Raising the alternate's price alone would breach
    `max_unit_price_premium_pct` and the plan would be refused before an
    approval existed, which would test the wrong thing. So the baseline on the
    original order moves with it.
    """
    supplier = harness.store.document("suppliers", "S-Z")
    supplier["pricing"]["P-4471"] = new_price
    harness.store.put_document("suppliers", "S-Z", supplier)

    original = harness.store.document("purchase_orders", "PO-77812")
    original["unit_price"] = new_price * 0.95
    harness.store.put_document("purchase_orders", "PO-77812", original)


def test_a_backup_without_the_authority_escalates_instead(harness):
    """Marcus may approve up to 20,000. Make the order worth more than that and
    the request must go to Dana's manager, not to a desk that cannot act."""
    _reprice(harness, 60.0)

    _pending(harness)
    harness.clock.advance_to("2026-09-02T17:00:00")
    moved = harness.approvals.reroute_stale()

    assert moved[0]["requested_of"] == "u-100", "should escalate past the backup"
    assert "may approve up to" in moved[0]["routed_reason"]


def test_only_the_person_asked_may_answer(harness):
    record = _pending(harness)
    with pytest.raises(PermissionError, match="was asked of"):
        harness.approvals.decide(record["id"], verdict="approved", by="u-102")


def test_the_original_approver_loses_the_right_after_rerouting(harness):
    record = _pending(harness)
    harness.clock.advance_to("2026-09-02T17:00:00")
    harness.approvals.reroute_stale()

    with pytest.raises(PermissionError):
        harness.approvals.decide(record["id"], verdict="approved", by="u-101")
    decided = harness.approvals.decide(record["id"], verdict="approved", by="u-102")
    assert decided["decided_by"] == "u-102"


def test_an_approver_cannot_exceed_their_own_limit(harness):
    """Checked at the moment of the decision, not only at routing time."""
    _reprice(harness, 100.0)

    record = _pending(harness)
    assert record["requested_of"] == "u-100", "40,000 is already above Dana's limit"

    harness.store.conn.execute(
        "update approvals set requested_of = 'u-102' where id = ?", (record["id"],)
    )
    with pytest.raises(PermissionError, match="may approve up to"):
        harness.approvals.decide(record["id"], verdict="approved", by="u-102")


def test_a_decision_cannot_be_made_twice(harness):
    record = _pending(harness)
    harness.approvals.decide(record["id"], verdict="approved", by="u-101")
    with pytest.raises(ValueError, match="already approved"):
        harness.approvals.decide(record["id"], verdict="rejected", by="u-101")


# -- execution needs a real approval --------------------------------------


def test_execution_refuses_a_pending_approval(harness):
    record = _pending(harness)
    result = harness.execute(record["id"])

    assert result.status == "not_approved"
    orders = harness.store.documents("purchase_orders")
    assert [o["po_id"] for o in orders if o["part_id"] == "P-4471"] == ["PO-77812"]


def test_execution_refuses_a_rejected_approval(harness):
    record = _pending(harness)
    harness.approvals.decide(record["id"], verdict="rejected", by="u-101",
                             note="Talk to Kestrel first.")
    result = harness.execute(record["id"])

    assert result.status == "not_approved"
    assert harness.store.document("purchase_orders", "PO-77812")["status"] == "open"


def test_a_rejection_is_recorded_with_its_note(harness):
    record = _pending(harness)
    harness.approvals.decide(record["id"], verdict="rejected", by="u-101",
                             note="Kestrel have recovered before, hold off.")
    entry = next(e for e in harness.audit.entries()
                 if e["action"] == "approval.rejected")
    assert entry["detail"]["note"].startswith("Kestrel have recovered")
