#!/usr/bin/env python3
"""Silo command line.

    python silo.py demo             the whole story, end to end
    python silo.py init             seed the world
    python silo.py detect           run the detector sweep
    python silo.py run <item|--all> take attention items to a decision
    python silo.py approvals        what is waiting, and with whom
    python silo.py approve <id> --as <user>
    python silo.py reject  <id> --as <user> [--note "..."]
    python silo.py execute <approval-id> [--stop-before <step>]
    python silo.py resume <instance-id> --as <user>
    python silo.py tick             reroute stale approvals, fire due work
    python silo.py clock [--to DATE | --days N]
    python silo.py audit [<run-id>]
    python silo.py verify           check the audit hash chain
    python silo.py catalogue        tools, providers, detectors, workflows

No UI, by design. The approval step is a command, which is the brief's "a CLI
is fine" taken literally: what matters is that a human decision is a separate,
recorded event, not that it arrives through a web page.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import console
from harness import detect, providers, tools, workflows
from harness.kernel import Harness

ROOT = Path(__file__).parent
_P = console.setup()
BOLD, DIM, RESET = _P.BOLD, _P.DIM, _P.RESET
GREEN, RED, YELLOW, CYAN = _P.GREEN, _P.RED, _P.YELLOW, _P.CYAN


def heading(text: str) -> None:
    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}{text}{RESET}")
    print(f"{BOLD}{'=' * 78}{RESET}")


def step(text: str) -> None:
    print(f"\n{CYAN}-- {text}{RESET}")


def note(text: str) -> None:
    print(f"{DIM}   {text}{RESET}")


def ok(text: str) -> None:
    print(f"{GREEN}   {text}{RESET}")


def bad(text: str) -> None:
    print(f"{RED}   {text}{RESET}")


def warn(text: str) -> None:
    print(f"{YELLOW}   {text}{RESET}")


def open_harness(args) -> Harness:
    return Harness(
        db_path=args.db,
        company_dir=ROOT / "company",
        runs_dir=args.runs,
        cassette_dir=ROOT / "cassettes",
    )


# -- individual commands --------------------------------------------------


def cmd_init(args) -> int:
    if args.fresh:
        for path in (Path(args.db), Path(f"{args.db}-wal"), Path(f"{args.db}-shm")):
            path.unlink(missing_ok=True)
        shutil.rmtree(args.runs, ignore_errors=True)
    harness = open_harness(args)
    harness.initialise()
    print(f"seeded from company/ at {harness.clock.iso()}")
    harness.close()
    return 0


def cmd_detect(args) -> int:
    harness = open_harness(args)
    result = harness.detect()
    print(f"{len(result.new_items)} new, {len(result.suppressed)} suppressed, "
          f"{len(result.skipped)} skipped")
    for item in result.new_items:
        print(f"\n  {BOLD}{item.id}{RESET}  {item.detector}  for {item.subject_user}")
        print(f"  {item.summary}")
    for suppressed in result.suppressed:
        note(f"suppressed {suppressed['dedupe_key']} "
             f"(already open as {suppressed['existing_item']})")
    harness.close()
    return 0


def cmd_run(args) -> int:
    harness = open_harness(args)
    if args.all:
        rows = harness.store.conn.execute(
            "select id from attention_items where status = 'open' order by created_at"
        ).fetchall()
        item_ids = [r["id"] for r in rows]
    else:
        item_ids = [args.item]

    for item_id in item_ids:
        item = detect.load_item(harness.store, item_id)
        if item is None:
            bad(f"no such attention item: {item_id}")
            continue
        result = harness.handle(item)
        _print_run(result)
    harness.close()
    return 0


def _print_run(result) -> None:
    print(f"\n  run {result.run_id}  [{result.status}]")
    if result.plan:
        print(f"\n  {BOLD}{result.plan['headline']}{RESET}\n")
        if result.plan.get("workflow"):
            note(f"proposes workflow {result.plan['workflow']} with "
                 f"{json.dumps(result.plan['workflow_params'])}")
        for action in result.plan.get("actions", []):
            note(f"proposes {action['tool']}: {action['rationale']}")
        if result.plan.get("no_action_reason"):
            note(f"no action: {result.plan['no_action_reason']}")
        for citation in result.plan.get("citations", []):
            note(f"cites {citation['system']}/{citation['record_id']}: "
                 f"{citation['claim']}")
    if result.gate:
        for check in result.gate["checks"]:
            line = f"{check['rule']}: {check['message']}"
            (ok if check["verdict"] == "pass" else
             bad if check["verdict"] == "fail" else note)(line)
    if result.approval:
        warn(f"awaiting approval {result.approval['id']} from "
             f"{result.approval['requested_of']} by "
             f"{result.approval['deadline']}")
    for message in result.messages:
        bad(message)


def cmd_approvals(args) -> int:
    harness = open_harness(args)
    pending = harness.approvals.pending()
    if not pending:
        print("nothing is waiting")
    for record in pending:
        who = harness.store.principal(record["requested_of"])
        print(f"\n  {BOLD}{record['id']}{RESET}  with {who.name} ({who.user_id})")
        print(f"  value {record['payload'].get('value', 0):.2f}  "
              f"deadline {record['deadline']}")
        if record["routed_reason"]:
            warn(record["routed_reason"])
        print(f"\n  {record['payload']['headline']}\n")
    harness.close()
    return 0


def cmd_decide(args, verdict: str) -> int:
    harness = open_harness(args)
    try:
        record = harness.approvals.decide(
            args.approval, verdict=verdict, by=getattr(args, "as_user"),
            note=getattr(args, "note", "") or "",
        )
        ok(f"{record['id']} {record['status']} by {record['decided_by']}")
        code = 0
    except (PermissionError, ValueError) as error:
        bad(str(error))
        code = 1
    harness.close()
    return code


def cmd_execute(args) -> int:
    harness = open_harness(args)
    result = harness.execute(args.approval, stop_before=args.stop_before)
    _print_execution(result)
    harness.close()
    return 0 if result.status in ("completed", "interrupted") else 1


def _print_execution(result) -> None:
    execution = result.execution or {}
    print(f"\n  run {result.run_id}  [{result.status}]")
    for entry in execution.get("steps", []):
        label = entry.get("step_id") or entry.get("tool")
        status = entry.get("status") or ("ok" if entry.get("ok") else "failed")
        (ok if status == "ok" else bad)(f"{label}: {status}")
        if entry.get("error"):
            bad(f"    {json.dumps(entry['error'])}")
    for compensation in execution.get("compensations", []):
        warn(f"compensated {compensation.get('step_id')}: "
             f"reversed={compensation.get('effect_reversed')} "
             f"{compensation.get('note', '')}")


def cmd_resume(args) -> int:
    harness = open_harness(args)
    principal = harness.store.principal(getattr(args, "as_user"))
    outcome = harness.engine.run(
        args.instance, scoped_store=harness.store.scoped(principal)
    )
    print(f"  {args.instance}: {outcome.status}")
    for entry in outcome.steps:
        (ok if entry["status"] == "ok" else bad)(
            f"{entry['index']} {entry['step_id']}: {entry['status']}"
        )
    harness.close()
    return 0


def cmd_tick(args) -> int:
    harness = open_harness(args)
    result = harness.tick()
    print(json.dumps(result, indent=2))
    harness.close()
    return 0


def cmd_clock(args) -> int:
    harness = open_harness(args)
    if args.to:
        when = args.to if "T" in args.to else f"{args.to}T09:00:00"
        harness.clock.advance_to(when)
        harness.audit.record(phase="clock", action="clock.advanced", actor="operator",
                             detail={"to": harness.clock.iso()})
    elif args.days:
        harness.clock.advance(days=args.days)
        harness.audit.record(phase="clock", action="clock.advanced", actor="operator",
                             detail={"to": harness.clock.iso()})
    print(harness.clock.iso())
    harness.close()
    return 0


def cmd_audit(args) -> int:
    harness = open_harness(args)
    if args.run:
        print(harness.audit.transcript(args.run))
    else:
        for entry in harness.audit.entries():
            print(f"[{entry['seq']:>4}] {entry['ts']} {entry['phase']:<9} "
                  f"{entry['action']:<28} {entry['actor']}")
    harness.close()
    return 0


def cmd_verify(args) -> int:
    harness = open_harness(args)
    try:
        count = harness.audit.verify()
        ok(f"audit chain verified across {count} entries")
        code = 0
    except Exception as error:  # noqa: BLE001
        bad(str(error))
        code = 1
    harness.close()
    return code


def cmd_catalogue(args) -> int:
    print(f"\n{BOLD}Providers{RESET}")
    for name, provider in sorted(providers.all_providers().items()):
        print(f"  {name:<10} {provider.description}")
        note(f"scopes: {', '.join(sorted(provider.scopes))}")
    print(f"\n{BOLD}Detectors{RESET}")
    for name, det in sorted(detect.all_detectors().items()):
        print(f"  {name}")
        note(det.description)
        note(f"roles: {', '.join(sorted(det.roles))}")
    print(f"\n{BOLD}Tools{RESET}")
    for entry in tools.catalogue():
        print(f"  {entry['name']:<30} {entry['description'][:60]}")
        note(f"scopes: {', '.join(entry['required_scopes']) or 'none'}  "
             f"reversible: {entry['reversible']}")
    print(f"\n{BOLD}Workflows{RESET}")
    for entry in workflows.catalogue():
        print(f"  {entry['name']} v{entry['version']}")
        for index, s in enumerate(entry["steps"], 1):
            note(f"{index}. {s['id']} ({s['kind']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="silo", description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "silo.db"))
    parser.add_argument("--runs", default=str(ROOT / "runs"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.add_argument("--fresh", action="store_true")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("detect").set_defaults(fn=cmd_detect)

    p = sub.add_parser("run")
    p.add_argument("item", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_run)

    sub.add_parser("approvals").set_defaults(fn=cmd_approvals)

    for name, verdict in (("approve", "approved"), ("reject", "rejected")):
        p = sub.add_parser(name)
        p.add_argument("approval")
        p.add_argument("--as", dest="as_user", required=True)
        p.add_argument("--note", default="")
        p.set_defaults(fn=lambda a, v=verdict: cmd_decide(a, v))

    p = sub.add_parser("execute")
    p.add_argument("approval")
    p.add_argument("--stop-before", default=None,
                   help="simulate a crash before this workflow step")
    p.set_defaults(fn=cmd_execute)

    p = sub.add_parser("resume")
    p.add_argument("instance")
    p.add_argument("--as", dest="as_user", required=True)
    p.set_defaults(fn=cmd_resume)

    sub.add_parser("tick").set_defaults(fn=cmd_tick)

    p = sub.add_parser("clock")
    p.add_argument("--to", default=None)
    p.add_argument("--days", type=int, default=0)
    p.set_defaults(fn=cmd_clock)

    p = sub.add_parser("audit"); p.add_argument("run", nargs="?")
    p.set_defaults(fn=cmd_audit)

    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    sub.add_parser("catalogue").set_defaults(fn=cmd_catalogue)

    p = sub.add_parser("demo")
    p.add_argument("--scripted", action="store_true",
                   help="use the scripted model client instead of the API")
    p.set_defaults(fn=lambda a: __import__("demo").run(a))

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
