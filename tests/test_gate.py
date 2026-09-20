"""The gate. Required by the brief, and the layer most worth distrusting.

These tests are written the way an authorization test should be: they assert
that the thing is *refused*, and that the world is unchanged afterwards. A gate
test that only checks a boolean is checking that a function returns, not that a
purchase order failed to exist.
"""
from __future__ import annotations

import pytest
from conftest import to_approval

from harness.errors import ScopeDenied
from harness.plan.schema import Plan


def _plan(**overrides) -> Plan:
    base = {
        "headline": "A headline long enough to satisfy the schema's minimum length.",
        "reasoning": "Reasoning long enough to satisfy the schema's minimum length rule.",
        "citations": [],
        "workflow": None,
        "workflow_params": {},
        "actions": [],
        "no_action_reason": None,
        "confidence": "medium",
    }
    return Plan.model_validate(base | overrides)


def _reroute_params(**overrides) -> dict:
    return {
        "part_id": "P-4471",
        "original_po_id": "PO-77812",
        "prod_order_id": "4812",
        "qty": 400,
        "needed_by": "2026-09-07",
        "preferred_supplier_id": None,
        "justification": "The open order will not arrive before 4812 starts.",
    } | overrides


def evaluate(harness, plan, user="u-101"):
    principal = harness.store.principal(user)
    return harness.gate.evaluate(
        plan, principal=principal, scoped_store=harness.store.scoped(principal),
        run_id="test-run",
    )


# -- permissions ----------------------------------------------------------


def test_refuses_a_tool_the_user_has_no_scope_for(harness):
    """The quality manager has no purchase order scopes at all."""
    plan = _plan(actions=[{
        "tool": "create_purchase_order",
        "params": {"part_id": "P-4471", "supplier_id": "S-Z", "qty": 10,
                   "unit_price": 46.5, "needed_by": "2026-09-07",
                   "reason": "should never be permitted for this user"},
        "rationale": "Attempting a purchase as the quality manager.",
    }])
    decision = evaluate(harness, plan, user="u-202")

    assert decision.allowed is False
    assert "permissions.scopes" in [c.rule for c in decision.failures]
    assert decision.approver_id is None, "a refused plan must not name an approver"


def test_refuses_a_tool_that_does_not_exist(harness):
    plan = _plan(actions=[{
        "tool": "wire_money_offshore", "params": {},
        "rationale": "A tool the model invented out of nothing.",
    }])
    decision = evaluate(harness, plan)
    assert decision.allowed is False
    assert "permissions.tool_exists" in [c.rule for c in decision.failures]


def test_every_rule_runs_even_after_one_fails(harness):
    """No short-circuiting. Somebody fixing the first problem should already
    know about the second."""
    plan = _plan(actions=[{
        "tool": "create_purchase_order",
        "params": {"part_id": "P-4471", "supplier_id": "S-Q", "qty": 400,
                   "unit_price": 38.90, "needed_by": "2026-09-07",
                   "reason": "the cheap supplier that is not approved for this part"},
        "rationale": "Choosing the cheapest quote on file.",
    }], workflow=None)
    decision = evaluate(harness, plan, user="u-202")

    rules = [c.rule for c in decision.checks]
    assert "permissions.scopes" in rules
    assert "purchase_orders.supplier_must_be_approved_for_part" in rules
    assert len(decision.failures) >= 2


# -- policy ---------------------------------------------------------------


def test_refuses_a_supplier_not_approved_for_this_part(harness):
    """S-Q is genuinely an approved supplier. It is not approved for P-4471.

    This is the trap the seed exists to set. A gate that reads `approved` and
    stops there lets it through.
    """
    plan = _plan(actions=[{
        "tool": "create_purchase_order",
        "params": {"part_id": "P-4471", "supplier_id": "S-Q", "qty": 400,
                   "unit_price": 38.90, "needed_by": "2026-09-07",
                   "reason": "cheapest quote on file and ships next day"},
        "rationale": "Apex are cheaper and faster than every alternative.",
    }])
    decision = evaluate(harness, plan)

    failure = next(c for c in decision.failures
                   if c.rule == "purchase_orders.supplier_must_be_approved_for_part")
    assert decision.allowed is False
    assert "S-Q" in failure.message
    offending = failure.detail["offending"][0]
    assert offending["approved"] is True, "the supplier really is approved in general"
    assert "P-4471" not in offending["approved_parts"]


def test_the_gate_reads_the_world_not_the_plan(harness):
    """A plan that asserts something false does not get to be believed.

    The supplier is disapproved in the store after the plan was written, and
    the free-form action still names it. The gate must refuse on what the
    record says now, not on what the plan claims.
    """
    supplier = harness.store.document("suppliers", "S-Z")
    supplier["approved"] = False
    harness.store.put_document("suppliers", "S-Z", supplier)

    plan = _plan(actions=[{
        "tool": "create_purchase_order",
        "params": {"part_id": "P-4471", "supplier_id": "S-Z", "qty": 400,
                   "unit_price": 46.50, "needed_by": "2026-09-07",
                   "reason": "the plan believes this supplier is still approved"},
        "rationale": "Meridian were approved when this plan was written.",
    }])
    decision = evaluate(harness, plan)

    assert decision.allowed is False
    assert "purchase_orders.supplier_must_be_approved_for_part" in [
        c.rule for c in decision.failures
    ]


def test_a_free_form_purchase_is_valued_and_escalated(harness):
    """The approval threshold is not a workflow feature.

    A plan that reaches for the tool directly has to be valued and routed the
    same way, or the threshold is trivially avoidable by not using a workflow.
    """
    plan = _plan(actions=[{
        "tool": "create_purchase_order",
        "params": {"part_id": "P-4471", "supplier_id": "S-Z", "qty": 4000,
                   "unit_price": 46.50, "needed_by": "2026-09-07",
                   "reason": "a large order placed outside any declared workflow"},
        "rationale": "Ordering well above the manager's own limit.",
    }])
    decision = evaluate(harness, plan)

    assert decision.allowed is True
    assert decision.facts["worst_case_value"] == 186000.0
    assert decision.approver_id == "u-100", "must escalate past Dana's 25,000 limit"
    assert decision.routing["escalated"] is True


def test_refuses_when_no_approved_supplier_can_make_the_date(harness):
    """Leave only the slow approved supplier standing."""
    supplier = harness.store.document("suppliers", "S-Z")
    supplier["approved_parts"] = []
    harness.store.put_document("suppliers", "S-Z", supplier)

    plan = _plan(workflow="po_reroute", workflow_params=_reroute_params())
    decision = evaluate(harness, plan)

    failure = next(
        c for c in decision.failures
        if c.rule == "purchase_orders.an_approved_supplier_can_make_the_date"
    )
    assert decision.allowed is False
    assert failure.detail["reachable"] == []


def test_refuses_a_lot_that_is_on_hold(harness):
    plan = _plan(actions=[{
        "tool": "reallocate_lot",
        "params": {"prod_order_id": "4820", "part_id": "P-1180",
                   "from_lot": "L-2101", "to_lot": "L-2093",
                   "reason": "reallocating onto the lot that is on hold"},
        "rationale": "Moving production onto the held lot.",
    }])
    decision = evaluate(harness, plan, user="u-202")

    assert decision.allowed is False
    assert "quality.substitute_lot_must_be_released" in [
        c.rule for c in decision.failures
    ]


def test_refuses_a_lot_already_allocated_elsewhere(harness):
    plan = _plan(actions=[{
        "tool": "reallocate_lot",
        "params": {"prod_order_id": "4820", "part_id": "P-1180",
                   "from_lot": "L-2093", "to_lot": "L-2088",
                   "reason": "reallocating onto a lot committed to another order"},
        "rationale": "L-2088 is released, so it looks available.",
    }])
    decision = evaluate(harness, plan, user="u-202")

    assert decision.allowed is False
    assert "quality.substitute_lot_must_be_unallocated" in [
        c.rule for c in decision.failures
    ]


# -- approval requirement and routing -------------------------------------


def test_a_write_always_requires_a_human(harness):
    plan = _plan(workflow="po_reroute", workflow_params=_reroute_params())
    decision = evaluate(harness, plan)

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.approver_id == "u-101"


def test_no_approval_is_asked_for_when_nothing_is_written(harness):
    plan = _plan(no_action_reason="The shipment arrived, so there is nothing to do.")
    decision = evaluate(harness, plan)

    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.approver_id is None


def test_value_above_the_users_limit_escalates_to_their_manager(harness):
    """Dana's limit is 25,000. Four thousand units at Meridian's price is not."""
    plan = _plan(workflow="po_reroute", workflow_params=_reroute_params(qty=4000))
    decision = evaluate(harness, plan)

    assert decision.allowed is True
    assert decision.approver_id == "u-100", "should go to Dana's manager"
    assert decision.routing["escalated"] is True
    assert decision.facts["worst_case_value"] > 25000


def test_the_gate_values_a_workflow_at_its_worst_case(harness):
    """Approval is granted before the supplier is chosen, so the value the
    approver sees has to be the most expensive candidate, not the cheapest."""
    plan = _plan(workflow="po_reroute", workflow_params=_reroute_params())
    decision = evaluate(harness, plan)

    prices = []
    for supplier_id in decision.facts["supplier_candidates"]:
        record = harness.store.document("suppliers", supplier_id)
        prices.append(record["pricing"]["P-4471"])
    assert decision.facts["worst_case_unit_price"] == max(prices)


# -- the gate is not the only defence -------------------------------------


def test_the_store_refuses_even_if_the_gate_were_bypassed(harness):
    """Defence in depth. Call the tool runner directly, with no gate involved."""
    dana = harness.store.scoped(harness.store.principal("u-101"))
    result = harness.runner.invoke(
        "reallocate_lot",
        {"prod_order_id": "4820", "part_id": "P-1180", "from_lot": "L-2093",
         "to_lot": "L-2101", "reason": "bypassing the gate entirely"},
        scoped_store=dana, run_id="test-run",
    )
    assert result.ok is False
    assert result.error["error"] == "ScopeDenied"

    order = harness.store.document("production_orders", "4820")
    component = next(c for c in order["components"] if c["part_id"] == "P-1180")
    assert component["allocated_lots"] == ["L-2093"], "nothing moved"


def test_a_provider_cannot_read_past_its_principal(harness):
    """Not a gate test exactly, and the same guarantee: the handle cannot
    express the query."""
    dana = harness.store.scoped(harness.store.principal("u-101"))
    with pytest.raises(ScopeDenied):
        dana.quality_lots()

    elena = harness.store.scoped(harness.store.principal("u-202"))
    assert "M-005" in {m["message_id"] for m in elena.inbox()}
    assert "M-005" not in {m["message_id"] for m in dana.inbox()}, (
        "a message addressed only to the quality manager reached purchasing"
    )


def test_a_refused_plan_never_reaches_an_approver(harness):
    """End to end: the run stops, and no approval row exists."""
    supplier = harness.store.document("suppliers", "S-Z")
    supplier["approved_parts"] = []
    harness.store.put_document("suppliers", "S-Z", supplier)

    result = to_approval(harness)
    assert result.status == "refused"
    assert result.approval is None
    assert harness.approvals.pending() == []
