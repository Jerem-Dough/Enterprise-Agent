"""The audit log: append only, tamper evident, and sufficient on its own.

The last of those is the one the brief actually asks for. "From the audit log
alone, someone should be able to reconstruct what the agent saw, what it
concluded, what it was allowed to do, who approved what, and what actually
happened in each system." So the interesting test is not that entries exist, it
is that a reader holding only the log can answer all five questions.
"""
from __future__ import annotations

import sqlite3

import pytest
from conftest import approve_and_execute, to_approval

from harness.errors import AuditTampered


# -- append only ----------------------------------------------------------


def test_the_database_refuses_an_update(harness):
    harness.detect()
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        harness.store.conn.execute(
            "update audit_log set actor = 'somebody else' where seq = 1"
        )


def test_the_database_refuses_a_delete(harness):
    harness.detect()
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        harness.store.conn.execute("delete from audit_log where seq = 1")


def test_the_privileged_handle_is_refused_too(harness):
    """The trigger fires for every connection.

    The point of putting it in the schema rather than in application code is
    that a bug in the harness cannot rewrite history either.
    """
    harness.detect()
    with pytest.raises(sqlite3.IntegrityError):
        with harness.store.transaction() as conn:
            conn.execute("update audit_log set phase = 'detect' where seq = 1")


def test_reseeding_does_not_reset_the_log(harness):
    """Everything else can be rebuilt from `company/`. The log cannot."""
    harness.detect()
    before = len(harness.audit.entries())
    harness.initialise()
    assert len(harness.audit.entries()) > before


# -- tamper evidence ------------------------------------------------------


def test_the_chain_verifies_on_a_clean_log(harness):
    approve_and_execute(harness)
    assert harness.audit.verify() > 20


def test_the_chain_catches_an_edit_made_with_the_trigger_dropped(harness):
    approve_and_execute(harness)
    harness.store.conn.execute("drop trigger audit_log_is_append_only_update")
    harness.store.conn.execute("update audit_log set actor = 'mallory' where seq = 3")

    with pytest.raises(AuditTampered, match="entry 3"):
        harness.audit.verify()


def test_the_chain_names_where_it_broke_and_says_the_rest_is_good(harness):
    approve_and_execute(harness)
    harness.store.conn.execute("drop trigger audit_log_is_append_only_delete")
    harness.store.conn.execute("delete from audit_log where seq = 5")

    with pytest.raises(AuditTampered) as caught:
        harness.audit.verify()
    assert "entries before it verify" in str(caught.value)


# -- reconstructability ---------------------------------------------------


def test_the_log_alone_answers_all_five_questions(harness):
    execution = approve_and_execute(harness)
    entries = harness.audit.entries(execution.run_id)
    actions = {e["action"] for e in entries}
    by_action = {e["action"]: e for e in entries}

    # 1. What the agent saw.
    assert "context.gathered" in actions
    gathered = by_action["context.gathered"]["detail"]
    assert gathered["providers_used"] == ["calendar", "erp", "mail"]
    assert gathered["providers_skipped"][0]["provider"] == "quality"

    # 2. What it concluded, and from what context.
    assert "plan.produced" in actions
    plan = by_action["plan.produced"]["detail"]
    assert plan["workflow"] == "po_reroute"
    assert plan["citations"], "a conclusion with no citations is unreconstructable"
    assert plan["context_window_chars"]["system_chars"] > 0

    # 3. What it was allowed to do.
    assert "gate.allowed" in actions
    gate = by_action["gate.allowed"]["detail"]
    assert gate["required_scopes"]
    assert [c["rule"] for c in gate["checks"]]

    # 4. Who approved what.
    assert "approval.requested" in actions
    assert any(a.startswith("approval.approved") for a in actions)

    # 5. What actually happened in each system.
    invocations = [e for e in entries if e["action"] == "tool.invoked"]
    assert {e["entity_id"] for e in invocations} == {
        "create_purchase_order", "amend_purchase_order",
        "notify_production", "schedule_follow_up",
    }
    for entry in invocations:
        assert entry["detail"]["idempotency_key"]
        assert entry["detail"]["output"]
        assert entry["detail"]["rationale"]


def test_a_refusal_records_the_rule_that_refused(harness):
    supplier = harness.store.document("suppliers", "S-Z")
    supplier["approved_parts"] = []
    harness.store.put_document("suppliers", "S-Z", supplier)

    result = to_approval(harness)
    refusal = next(e for e in harness.audit.entries(result.run_id)
                   if e["action"] == "gate.refused")

    assert refusal["detail"]["failed_rules"]
    failing = [c for c in refusal["detail"]["checks"] if c["verdict"] == "fail"]
    assert failing[0]["message"], "a refusal with no message is not actionable"
    assert failing[0]["detail"], "a refusal must carry the values it decided on"


def test_a_denied_tool_call_is_recorded_not_swallowed(harness):
    dana = harness.store.scoped(harness.store.principal("u-101"))
    harness.runner.invoke(
        "reallocate_lot",
        {"prod_order_id": "4820", "part_id": "P-1180", "from_lot": "L-2093",
         "to_lot": "L-2101", "reason": "an attempt that should be refused"},
        scoped_store=dana, run_id="test-run",
    )
    denied = next(e for e in harness.audit.entries("test-run")
                  if e["action"] == "tool.denied")
    assert denied["detail"]["missing_scopes"] == [
        "erp:quality:read", "erp:quality:reallocate",
    ], "every missing scope is recorded, not just the first one refused"


def test_rerouting_records_both_ends_and_the_reason(harness):
    to_approval(harness)
    harness.clock.advance_to("2026-09-02T17:00:00")
    harness.tick()

    rerouted = next(e for e in harness.audit.entries()
                    if e["action"] == "approval.rerouted")
    detail = rerouted["detail"]
    assert detail["from"] == "u-101"
    assert detail["to"] == "u-102"
    assert detail["approver_out_on"] == "2026-09-03"
    assert "end of day" in detail["reason"]


def test_the_transcript_is_built_from_the_log_and_nothing_else(harness):
    execution = approve_and_execute(harness)
    transcript = harness.audit.transcript(execution.run_id)

    for marker in ("detect", "context", "plan", "gate", "approval", "execute"):
        assert marker in transcript
    assert "po_reroute" in transcript

    # It carries only this run. A transcript that leaked a neighbouring run's
    # entries would be unusable as evidence about either of them.
    other = harness.audit.entries()
    foreign = [e for e in other if e["run_id"] not in (execution.run_id, None)]
    for entry in foreign:
        assert entry["action"] not in transcript or entry["run_id"] is None


def test_the_run_folder_mirrors_the_ledger(harness):
    """The files are a rendering. Deleting them loses readability, not state."""
    from pathlib import Path

    execution = approve_and_execute(harness)
    folder = Path(harness.runs_dir) / execution.run_id
    written = {p.name for p in folder.iterdir()}

    assert {"01-attention.json", "02-context.json", "03-prompt.json",
            "04-plan.json", "05-gate.json", "06-approval.json",
            "08-execution.json", "audit.jsonl"} <= written
    assert (folder / "07-steps").is_dir()
    assert len(list((folder / "07-steps").iterdir())) == 7


def test_the_prompt_artifact_shows_what_the_model_was_given(harness):
    """Requirement 7 says "what the agent saw". For the reasoning step, that
    means the context window, not a summary of it."""
    from pathlib import Path

    execution = approve_and_execute(harness)
    import json

    prompt = json.loads(
        (Path(harness.runs_dir) / execution.run_id / "03-prompt.json").read_text(
            encoding="utf-8"
        )
    )
    assert "po_reroute" in prompt["system"], "the workflow catalogue was shown"
    assert "reallocate_lot" not in prompt["system"], (
        "a tool Dana cannot run should never enter her context window"
    )
    assert "P-4471" in prompt["user"]
