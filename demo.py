"""The whole story in one command: `python silo.py demo`.

Scenario A from an unprompted detection through approval and execution, the
clock advanced to Tuesday with the follow-up firing, Scenario B, then the
failure cases. Everything it prints comes from the harness; nothing here
computes a result for display.

The failure cases run against their own throwaway databases, because each one
needs the world back at its starting state and because proving that a refusal
leaves nothing behind is only meaningful if nothing was behind it already.
"""
from __future__ import annotations

import json

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "tests"))

import console  # noqa: E402
from harness import detect  # noqa: E402
from harness.kernel import Harness  # noqa: E402
from harness.plan.llm import build_client  # noqa: E402

_P = console.setup()
BOLD, DIM, RESET = _P.BOLD, _P.DIM, _P.RESET
GREEN, RED, YELLOW, CYAN, BLUE = _P.GREEN, _P.RED, _P.YELLOW, _P.CYAN, _P.BLUE


def chapter(number: str, title: str) -> None:
    print(f"\n\n{BOLD}{BLUE}{'━' * 78}{RESET}")
    print(f"{BOLD}{BLUE} {number}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'━' * 78}{RESET}")


def beat(text: str) -> None:
    print(f"\n{CYAN}▸ {text}{RESET}")


def says(text: str) -> None:
    print(f"\n{BOLD}  “{text}”{RESET}\n")


def line(text: str) -> None:
    print(f"  {text}")


def dim(text: str) -> None:
    print(f"{DIM}  {text}{RESET}")


def good(text: str) -> None:
    print(f"{GREEN}  ✓ {text}{RESET}")


def fail(text: str) -> None:
    print(f"{RED}  ✗ {text}{RESET}")


def flag(text: str) -> None:
    print(f"{YELLOW}  ! {text}{RESET}")


def _scripted_or_live(scripted: bool, **script):
    """Use the real model unless the caller asked for the scripted client.

    The failure cases need a model that behaves in a specific wrong way on
    purpose, which is not something you can ask a real one for reliably. The
    main scenarios use the real path, replaying a cassette when one exists.
    """
    if not scripted:
        return build_client(ROOT / "cassettes")
    import stub_llm

    return stub_llm.combined(**script)


def _harness(name: str, *, scripted: bool = False, fresh: bool = True,
             **script) -> Harness:
    root = ROOT / "_demo" / name
    if fresh:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    harness = Harness(
        db_path=root / "silo.db",
        company_dir=ROOT / "company",
        runs_dir=root / "runs",
        cassette_dir=ROOT / "cassettes",
        llm=_scripted_or_live(scripted, **script),
    )
    if fresh:
        harness.initialise()
    return harness


def _show_plan(result) -> None:
    plan = result.plan
    says(plan["headline"])
    dim("reasoning: " + plan["reasoning"])
    print()
    for citation in plan["citations"]:
        dim(f"cites {citation['system']}/{citation['record_id']}: {citation['claim']}")
    if plan.get("workflow"):
        line(f"\n  proposes workflow {BOLD}{plan['workflow']}{RESET} with "
             f"{json.dumps(plan['workflow_params'])}")
    for action in plan.get("actions", []):
        line(f"\n  proposes {BOLD}{action['tool']}{RESET}: {action['rationale']}")


def _show_gate(result) -> None:
    for check in result.gate["checks"]:
        (good if check["verdict"] == "pass" else fail)(
            f"{check['rule']}: {check['message']}"
        )


def _show_steps(result) -> None:
    for entry in (result.execution or {}).get("steps", []):
        label = entry.get("step_id") or entry.get("tool")
        status = entry.get("status") or ("ok" if entry.get("ok") else "failed")
        (good if status == "ok" else fail)(f"{label}")
        if entry.get("error"):
            fail(f"    {json.dumps(entry['error'])}")


# -- the main line --------------------------------------------------------


def scenario_a(harness: Harness) -> dict:
    chapter("1", "Scenario A, nobody asked for any of this")

    beat("A detector sweep runs on a schedule. Nobody prompted it.")
    sweep = harness.detect()
    for item in sweep.new_items:
        who = harness.store.principal(item.subject_user)
        line(f"\n  {BOLD}{item.id}{RESET}  {item.detector}")
        line(f"  for {who.name}, {who.role}")
        line(f"  {item.summary}")
    dim("\n  Two suppliers wrote to Dana about parts they hold open orders for.")
    dim("  Only one of those parts is tight, so only one became an item. The other")
    dim("  was Apex Rapid Components touting a cheaper stepper motor (M-004).")

    item = next(i for i in sweep.new_items
                if i.detector == "supplier_delay_threatens_production")

    beat("Gather from ERP, inbox and calendar, scoped to Dana, then reason.")
    result = harness.handle(item)
    _show_plan(result)

    beat("The gate. Every rule runs, every rule is named, enforced in code.")
    _show_gate(result)

    beat("Nothing is written until a person says so.")
    approval = result.approval
    who = harness.store.principal(approval["requested_of"])
    flag(f"approval {approval['id']} is with {who.name} until "
         f"{approval['deadline']}")
    dim(f"  {approval['payload']['gate']['approval_reason']}")

    chapter("2", "The approver is out tomorrow and has not answered")

    beat("End of day arrives with the request still pending.")
    harness.clock.advance_to("2026-09-02T17:00:00")
    line(f"  clock is now {harness.clock.iso()}")
    tick = harness.tick()
    for rerouted in tick["rerouted_approvals"]:
        flag(f"{rerouted['approval']} → {rerouted['now_with']}")
        dim(f"  {rerouted['reason']}")

    beat("Authority does not transfer with the request. Dana tries anyway.")
    try:
        harness.approvals.decide(approval["id"], verdict="approved", by="u-101")
        fail("Dana approved a request that is no longer hers")
    except PermissionError as error:
        good(f"refused: {error}")

    beat("Marcus approves.")
    decided = harness.approvals.decide(
        approval["id"], verdict="approved", by="u-102",
        note="Confirmed with the line. Proceed.",
    )
    good(f"approved by {harness.store.principal(decided['decided_by']).name} "
         f"at {decided['decided_at']}")

    chapter("3", "Execution, as a declared workflow")

    beat("po_reroute v1. The planner chose to enter it and supplied parameters. "
         "It does not get to reorder, skip or add a step.")
    execution = harness.execute(approval["id"])
    _show_steps(execution)

    steps = {s["step_id"]: s for s in execution.execution["steps"]}
    choice = steps["select_alternate_supplier"]["output"]
    offered = [c["supplier_id"] for c in choice["candidates_offered"]]
    dim(f"\n  the model was offered {offered} and chose {choice['supplier_id']}")
    dim(f"  {choice['justification']}")
    dim(f"\n  Apex (S-Q) is approved, cheapest on file for this part, and ships")
    dim(f"  next day. It is not approved for P-4471, so it never reached the model.")

    new_po = steps["create_replacement_po"]["output"]
    line(f"\n  new order {BOLD}{new_po['po_id']}{RESET} with "
         f"{new_po['supplier_name']}, {new_po['qty']} at {new_po['unit_price']}, "
         f"promised {new_po['promised_date']}")
    line(f"  original {steps['amend_original_po']['output']['po_id']} is now "
         f"{steps['amend_original_po']['output']['after']['status']}")
    notify = steps["notify_production"]["output"]
    line(f"  notified {notify['to'][0]}")
    dim(f"  notification text came from "
        f"{'the deterministic fallback' if notify['used_fallback_text'] else 'the model, facts verified'}")
    check = steps["schedule_arrival_check"]["output"]
    line(f"  arrival check queued for {BOLD}{check['due_at']}{RESET}")

    return {"approval": approval["id"], "po_id": new_po["po_id"],
            "run_id": execution.run_id}


def follow_up(harness: Harness, state: dict) -> None:
    chapter("5", "The follow-up, and the clock advanced to Tuesday")

    dim("  The brief says the check is “for Tuesday”. In its own numbers Tuesday")
    dim("  is when the OLD supplier said their delayed shipment would land. The")
    dim("  replacement is promised sooner, so the check is scheduled against the")
    dim("  replacement, and on a miss it re-checks the next working day.")

    for target, label in (("2026-09-04", "Friday, the promised date"),
                          ("2026-09-07", "Monday, production order 4812 starts"),
                          ("2026-09-08", "Tuesday")):
        beat(f"Advance the clock to {target}, {label}.")
        harness.clock.advance_to(f"{target}T09:00:00")
        tick = harness.tick()
        if not tick["fired_tasks"]:
            dim("  nothing was due")
            continue
        for fired in tick["fired_tasks"]:
            outcome = fired["outcome"]
            if outcome["status"] == "arrived":
                good(f"{outcome['po_id']} has arrived")
            else:
                fail(f"{outcome['po_id']} is still open on {outcome['checked_on']}")
                line(f"  re-entered the loop as attention item "
                     f"{outcome['attention_item']}")
                line(f"  raised a new item: {outcome['raised_new_item']}"
                     f"   next check {outcome['next_check']}")

    dim("\n  The dedupe key is the order, not the day. One unresolved late")
    dim("  shipment is one attention item across all three checks, not three.")


def scenario_b(harness: Harness) -> None:
    chapter("4", "Scenario B, a different person, a different shape of problem")

    rows = harness.store.conn.execute(
        "select id from attention_items where detector = 'lot_hold_blocks_production'"
    ).fetchone()
    item = detect.load_item(harness.store, rows["id"])
    who = harness.store.principal(item.subject_user)
    line(f"  {who.name}, {who.role}. No purchase order scopes at all.")
    dim(f"  scopes: {', '.join(sorted(who.scopes))}")

    beat("Same kernel, same gate, same audit. A detector, a provider, two "
         "tools and a user were added, and nothing else changed.")
    result = harness.handle(item)
    _show_plan(result)

    beat("The gate runs different rules, because the plan does different things.")
    _show_gate(result)

    beat("Approve and execute. No workflow covers this, so it takes the "
         "free-form path.")
    harness.approvals.decide(
        result.approval["id"], verdict="approved",
        by=result.approval["requested_of"], note="Confirmed L-2101 on the floor.",
    )
    execution = harness.execute(result.approval["id"])
    _show_steps(execution)

    order = harness.store.scoped(who).production_order("4820")
    allocated = [c.get("allocated_lots") for c in order["components"]
                 if c["part_id"] == "P-1180"]
    good(f"production order 4820 now pulls {allocated}")


def failure_cases() -> None:
    chapter("6", "Failure cases")

    print(f"\n{BOLD}  6a. The model picks the supplier it was never offered{RESET}")
    dim("  A model that names Apex, the cheap unapproved supplier, anyway.")
    harness = _harness("bait", scripted=True, bad_choice=True)
    result = _to_approval(harness)
    execution = harness.execute(result.approval["id"])
    fail(f"workflow {execution.execution['status']}: "
         f"{execution.execution['error']['message']}")
    pos = harness.store.scoped(harness.store.principal("u-101")).purchase_orders(
        part_id="P-4471")
    good(f"nothing was written: {[(p['po_id'], p['status']) for p in pos]}")
    harness.close()

    print(f"\n{BOLD}  6b. The model writes an unusable notification{RESET}")
    dim("  A draft that sounds fine and carries none of the required facts.")
    harness = _harness("draft", scripted=True, bad_draft=True)
    result = _to_approval(harness)
    execution = harness.execute(result.approval["id"])
    notify = next(s for s in execution.execution["steps"]
                  if s["step_id"] == "notify_production")["output"]
    good(f"fell back to the template: {notify['fallback_reason']}")
    good(f"the workflow still completed: {execution.execution['status']}")
    harness.close()

    print(f"\n{BOLD}  6c. A tool run without the scope for it{RESET}")
    harness = _harness("scope", scripted=True)
    dana = harness.store.scoped(harness.store.principal("u-101"))
    denied = harness.runner.invoke(
        "reallocate_lot",
        {"prod_order_id": "4820", "part_id": "P-1180", "from_lot": "L-2093",
         "to_lot": "L-2101", "reason": "purchasing should not be able to do this"},
        scoped_store=dana, run_id="demo",
    )
    good(f"refused: {denied.error['message']}")
    dim("  and the provider layer refuses the read as well, so a plan that")
    dim("  needed it could never have been built from Dana's context bundle")
    harness.close()

    print(f"\n{BOLD}  6d. A killed process resumes without writing twice{RESET}")
    harness = _harness("resume", scripted=True)
    result = _to_approval(harness)
    interrupted = harness.execute(result.approval["id"],
                                  stop_before="amend_original_po")
    done = [s["step_id"] for s in interrupted.execution["steps"]]
    instance = interrupted.execution["instance_id"]
    flag(f"killed after {len(done)} steps: {done}")
    before = harness.store.scoped(
        harness.store.principal("u-101")).purchase_orders(part_id="P-4471")
    harness.close()

    resumed = _harness("resume", scripted=True, fresh=False)
    outcome = resumed.engine.run(
        instance, scoped_store=resumed.store.scoped(resumed.store.principal("u-101")),
        situation="resumed",
    )
    good(f"resumed in a new process: {outcome.status}, "
         f"{len(outcome.steps)} steps")
    after = resumed.store.scoped(
        resumed.store.principal("u-101")).purchase_orders(part_id="P-4471")
    good(f"orders before {len(before)}, after {len(after)}: no duplicate write")
    resumed.close()

    print(f"\n{BOLD}  6e. The same sweep twice{RESET}")
    harness = _harness("dedupe", scripted=True)
    first = harness.detect()
    second = harness.detect()
    good(f"first sweep {len(first.new_items)} new, "
         f"second sweep {len(second.new_items)} new, "
         f"{len(second.suppressed)} suppressed")
    harness.close()

    print(f"\n{BOLD}  6f. The audit log resists being rewritten{RESET}")
    harness = _harness("audit", scripted=True)
    harness.detect()
    import sqlite3
    for statement, label in (
        ("update audit_log set actor = 'nobody' where seq = 1", "UPDATE"),
        ("delete from audit_log where seq = 1", "DELETE"),
    ):
        try:
            harness.store.conn.execute(statement)
            fail(f"{label} succeeded")
        except sqlite3.IntegrityError as error:
            good(f"{label} refused by the database: {error}")
    harness.store.conn.execute("drop trigger audit_log_is_append_only_update")
    harness.store.conn.execute(
        "update audit_log set detail = '{}' where seq = 2")
    try:
        harness.audit.verify()
        fail("tampering went unnoticed")
    except Exception as error:  # noqa: BLE001
        good(f"and with the trigger dropped, the chain still catches it: {error}")
    harness.close()


def _to_approval(harness: Harness, subject: str = "u-101"):
    sweep = harness.detect()
    item = next(i for i in sweep.new_items if i.subject_user == subject)
    result = harness.handle(item)
    harness.approvals.decide(
        result.approval["id"], verdict="approved",
        by=result.approval["requested_of"],
    )
    return result


def audit_trail(harness: Harness, state: dict) -> None:
    chapter("7", "The audit, which is the actual deliverable")

    beat("Scenario A reconstructed from the log and nothing else.")
    print()
    print(harness.audit.transcript(state["run_id"]))

    beat("The whole chain.")
    count = harness.audit.verify()
    good(f"{count} entries, hash chain verified")
    line(f"\n  every run also left a folder you can read without running anything:")
    run_dir = Path(harness.runs_dir) / state["run_id"]
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            dim(f"  {path.relative_to(run_dir.parent)}")


def run(args) -> int:
    mode = "scripted" if getattr(args, "scripted", False) else "live or replayed"
    print(f"\n{BOLD}Silo, an extendable agent harness for enterprise work{RESET}")
    dim(f"model calls: {mode}")

    harness = _harness("main", scripted=getattr(args, "scripted", False))
    try:
        state = scenario_a(harness)
        # Both agents act on the same day. Only then does the clock move, so
        # Scenario B's production order has not already started when its own
        # agent reasons about it.
        scenario_b(harness)
        follow_up(harness, state)
        # Runs against its own throwaway databases, so the main world is
        # untouched and the audit chapter below still reconstructs the real run.
        failure_cases()
        audit_trail(harness, state)
    finally:
        harness.close()

    print(f"\n\n{BOLD}{GREEN}{'━' * 78}{RESET}")
    print(f"{BOLD}{GREEN} Done. Artifacts in _demo/main/runs/, audit in _demo/main/silo.db{RESET}")
    print(f"{BOLD}{GREEN}{'━' * 78}{RESET}\n")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--scripted", action="store_true",
                        help="use the scripted model client instead of the API")
    raise SystemExit(run(parser.parse_args()))
