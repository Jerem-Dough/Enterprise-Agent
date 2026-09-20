"""Workflow resumption, order, bounds and compensation. Required by the brief.

The resumption tests genuinely drop the harness and build a new one against the
same database file, because the claim is that nothing lives in memory. A test
that reused the object would prove only that a loop can continue.
"""
from __future__ import annotations

import pytest
from conftest import approve_and_execute, to_approval

from harness import workflows

STEPS = [
    "select_alternate_supplier",
    "confirm_supplier_approved",
    "confirm_lead_time",
    "create_replacement_po",
    "amend_original_po",
    "notify_production",
    "schedule_arrival_check",
]


def _approved(harness):
    result = to_approval(harness)
    harness.approvals.decide(
        result.approval["id"], verdict="approved",
        by=result.approval["requested_of"],
    )
    return result.approval["id"]


# -- the definition is in charge ------------------------------------------


def test_the_declared_order_is_what_runs(harness):
    execution = approve_and_execute(harness)
    assert [s["step_id"] for s in execution.execution["steps"]] == STEPS


def test_the_definition_is_immutable(harness):
    definition = workflows.get("po_reroute")
    assert isinstance(definition.steps, tuple)
    with pytest.raises((AttributeError, TypeError)):
        definition.steps[0] = definition.steps[1]


def test_a_plan_cannot_both_enter_a_workflow_and_add_actions(harness):
    """Enforced by the schema, before the gate ever sees the plan.

    Without this, a model could append an eighth step by putting it in
    `actions`, and the fixed order would hold only by convention.
    """
    from harness.plan.schema import Plan

    with pytest.raises(ValueError, match="exactly one of"):
        Plan.model_validate({
            "headline": "A headline long enough to satisfy the schema minimum.",
            "reasoning": "Reasoning long enough to satisfy the schema minimum rule.",
            "workflow": "po_reroute",
            "workflow_params": {},
            "actions": [{"tool": "notify_production", "params": {},
                         "rationale": "Sneaking in an extra step."}],
        })


# -- bounded model steps --------------------------------------------------


def test_the_model_only_sees_suppliers_approved_for_the_part(harness):
    execution = approve_and_execute(harness)
    choice = next(s for s in execution.execution["steps"]
                  if s["step_id"] == "select_alternate_supplier")["output"]
    offered = {c["supplier_id"] for c in choice["candidates_offered"]}

    assert "S-Q" not in offered, "the unapproved-for-this-part supplier was offered"
    assert "S-Y" not in offered, "the supplier that just slipped was offered"
    assert offered == {"S-Z", "S-W"}


def test_a_choice_outside_the_offered_set_fails_the_step(make_harness):
    harness = make_harness(bad_choice=True)
    execution = approve_and_execute(harness)

    assert execution.execution["status"] == "failed"
    assert execution.execution["error"]["error"] == "ChoiceOutOfBounds"
    assert "S-Q" in execution.execution["error"]["message"]


def test_a_refused_choice_writes_nothing(make_harness):
    harness = make_harness(bad_choice=True)
    approve_and_execute(harness)

    orders = harness.store.documents("purchase_orders")
    assert [o["po_id"] for o in orders if o["part_id"] == "P-4471"] == ["PO-77812"]
    assert harness.store.document("purchase_orders", "PO-77812")["status"] == "open"


def test_an_unusable_draft_falls_back_without_wedging(make_harness):
    """A language failure must not become an outage."""
    harness = make_harness(bad_draft=True)
    execution = approve_and_execute(harness)

    assert execution.execution["status"] == "completed"
    notify = next(s for s in execution.execution["steps"]
                  if s["step_id"] == "notify_production")["output"]
    assert notify["used_fallback_text"] is True
    assert "omitted" in notify["fallback_reason"]


def test_a_good_draft_is_used_and_its_facts_are_verified(harness):
    execution = approve_and_execute(harness)
    notify = next(s for s in execution.execution["steps"]
                  if s["step_id"] == "notify_production")["output"]
    assert notify["used_fallback_text"] is False

    sent = [m for m in harness.store.documents("messages")
            if m["message_id"] == notify["message_id"]][0]
    assert "4812" in sent["body"]
    assert "Meridian Drives" in sent["body"]


# -- resumption -----------------------------------------------------------


def test_a_killed_process_resumes_from_the_cursor(harness, reopen):
    approval_id = _approved(harness)
    interrupted = harness.execute(approval_id, stop_before="amend_original_po")

    assert interrupted.status == "interrupted"
    done = [s["step_id"] for s in interrupted.execution["steps"]]
    assert done == STEPS[:4]
    instance = interrupted.execution["instance_id"]
    harness.close()

    revived = reopen(0)
    outcome = revived.engine.run(
        instance,
        scoped_store=revived.store.scoped(revived.store.principal("u-101")),
        situation="resumed",
    )
    try:
        assert outcome.status == "completed"
        assert [s["step_id"] for s in outcome.steps] == STEPS
        assert all(s["status"] == "ok" for s in outcome.steps)
    finally:
        revived.close()


def test_resuming_does_not_write_twice(harness, reopen):
    approval_id = _approved(harness)
    interrupted = harness.execute(approval_id, stop_before="amend_original_po")
    instance = interrupted.execution["instance_id"]
    before = [o["po_id"] for o in harness.store.documents("purchase_orders")]
    harness.close()

    revived = reopen(0)
    try:
        revived.engine.run(
            instance,
            scoped_store=revived.store.scoped(revived.store.principal("u-101")),
            situation="resumed",
        )
        after = [o["po_id"] for o in revived.store.documents("purchase_orders")]
        assert sorted(after) == sorted(before), "resumption created a duplicate order"
    finally:
        revived.close()


def test_resumption_replays_completed_steps_by_idempotency_key(harness, reopen):
    """The key is the instance and step, not the arguments, so a recomputed
    parameter cannot cause a second write."""
    approval_id = _approved(harness)
    interrupted = harness.execute(approval_id, stop_before="amend_original_po")
    instance = interrupted.execution["instance_id"]
    harness.close()

    revived = reopen(0)
    try:
        keys = [
            r["idempotency_key"] for r in revived.store.conn.execute(
                "select idempotency_key from tool_invocations"
            )
        ]
        assert f"{instance}:create_replacement_po" in keys
    finally:
        revived.close()


def test_state_is_persisted_after_every_step(harness):
    approval_id = _approved(harness)
    harness.execute(approval_id, stop_before="notify_production")

    rows = harness.store.conn.execute(
        "select step_id, status from workflow_steps order by idx"
    ).fetchall()
    assert [r["step_id"] for r in rows] == STEPS[:5]
    assert all(r["status"] == "ok" for r in rows)

    instance = harness.store.conn.execute(
        "select cursor, status from workflow_instances"
    ).fetchone()
    assert instance["cursor"] == 5


def test_an_instance_refuses_to_resume_across_a_version_change(harness, monkeypatch):
    """Migrating in-flight instances is a design question. Refusing loudly is
    the honest floor, and silence would be the dangerous alternative."""
    approval_id = _approved(harness)
    interrupted = harness.execute(approval_id, stop_before="amend_original_po")
    instance = interrupted.execution["instance_id"]

    definition = workflows.get("po_reroute")
    bumped = workflows.WorkflowDefinition(
        name=definition.name, version="2.0.0", description=definition.description,
        params_model=definition.params_model, steps=definition.steps,
        gate_facts=definition.gate_facts,
    )
    monkeypatch.setitem(workflows.REGISTRY, "po_reroute", bumped)

    outcome = harness.engine.run(
        instance, scoped_store=harness.store.scoped(harness.store.principal("u-101"))
    )
    assert outcome.status == "failed"
    assert outcome.error["error"] == "WorkflowVersionMismatch"
    assert "1.0.0" in outcome.error["message"]


# -- compensation ---------------------------------------------------------


def test_a_late_failure_compensates_earlier_steps_in_reverse(harness, monkeypatch):
    """Break the last step and check the unwind."""
    definition = workflows.get("po_reroute")

    def explode(context):
        return workflows.StepOutcome(
            ok=False, error={"error": "Injected", "message": "the dock is on fire"}
        )

    broken = tuple(
        workflows.Step(s.id, s.description, s.kind, explode, s.tool)
        if s.id == "schedule_arrival_check" else s
        for s in definition.steps
    )
    monkeypatch.setitem(
        workflows.REGISTRY, "po_reroute",
        workflows.WorkflowDefinition(
            name=definition.name, version=definition.version,
            description=definition.description, params_model=definition.params_model,
            steps=broken, gate_facts=definition.gate_facts,
        ),
    )

    execution = approve_and_execute(harness)
    assert execution.execution["status"] == "failed"

    compensated = [c["step_id"] for c in execution.execution["compensations"]]
    assert compensated == list(reversed(STEPS[:6])), "must unwind in reverse order"

    assert harness.store.document("purchase_orders", "PO-77812")["status"] == "open", (
        "the original order should have been restored"
    )
    replacement = [o for o in harness.store.documents("purchase_orders")
                   if o["part_id"] == "P-4471" and o["po_id"] != "PO-77812"]
    assert replacement[0]["status"] == "cancelled"


def test_compensation_is_honest_about_what_it_could_not_undo(harness, monkeypatch):
    """A notification cannot be unsent. Saying otherwise in an audit log whose
    value is that it does not lie would be the worst possible bug."""
    definition = workflows.get("po_reroute")

    def explode(context):
        return workflows.StepOutcome(ok=False, error={"error": "Injected",
                                                      "message": "nope"})

    broken = tuple(
        workflows.Step(s.id, s.description, s.kind, explode, s.tool)
        if s.id == "schedule_arrival_check" else s
        for s in definition.steps
    )
    monkeypatch.setitem(
        workflows.REGISTRY, "po_reroute",
        workflows.WorkflowDefinition(
            name=definition.name, version=definition.version,
            description=definition.description, params_model=definition.params_model,
            steps=broken, gate_facts=definition.gate_facts,
        ),
    )

    execution = approve_and_execute(harness)
    notify = next(c for c in execution.execution["compensations"]
                  if c["step_id"] == "notify_production")

    assert notify["compensated"] is True
    assert notify["effect_reversed"] is False
    assert "cannot be unsent" in notify["note"]

    correction = [m for m in harness.store.documents("messages")
                  if m["subject"].startswith("Retracted:")]
    assert correction, "a correction should have gone out"
