"""The loop. Detect, gather, plan, gate, wait for a human, execute, follow up.

This module is small on purpose, and the size is the argument. Everything
domain-shaped lives in a registry: providers, detectors, tools, workflows. The
kernel knows the *order* of the phases and nothing about purchasing, quality,
suppliers or lots. Scenario B added a detector, a provider, two tools and a
user, and changed nothing in this file.

The phase boundaries are where the guarantees sit:

- nothing is gathered except through a provider holding a scoped handle
- nothing is planned except from a gathered bundle
- nothing is gated except a parsed, validated plan
- nothing is executed except a gated plan with a recorded human approval
- nothing happens at all without an audit entry, written in the same
  transaction as the effect

A run stops at the approval boundary and returns. It does not block, poll or
sleep. Resuming is a separate call with the approval id, which is what makes
the pause survive a restart: the pending state is a row, not a stack frame.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import detect, providers, tools, workflows
from .audit import AuditLog
from .clock import Clock
from .gate import Gate
from .gate.approvals import Approvals
from .memory import Memory
from .plan import Planner
from .plan.llm import LLMClient, build_client
from .plan.schema import Plan
from .principal import Principal
from .runlog import RunFolder
from .schedule import Scheduler
from .store import Store, new_id


def _next_working_day(day: date) -> date:
    """The next day somebody would be on a loading dock.

    Weekends only. Public holidays are a real gap and a real calendar would
    supply them; inventing a holiday table for a seeded demo would be modelling
    for its own sake.
    """
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


@dataclass
class RunResult:
    """Everything a caller needs to know about one pass through the loop."""

    run_id: str
    status: str
    attention_item: dict
    plan: dict | None = None
    gate: dict | None = None
    approval: dict | None = None
    execution: dict | None = None
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "attention_item": self.attention_item,
            "plan": self.plan,
            "gate": self.gate,
            "approval": self.approval,
            "execution": self.execution,
            "messages": self.messages,
        }


class Harness:
    """Wires the parts together and owns the phase order."""

    def __init__(
        self,
        db_path: str | Path = "silo.db",
        company_dir: str | Path = "company",
        runs_dir: str | Path = "runs",
        cassette_dir: str | Path = "cassettes",
        llm: LLMClient | None = None,
    ) -> None:
        self.company_dir = Path(company_dir)
        self.runs_dir = str(runs_dir)
        self.store = Store(db_path)
        self.clock = Clock(self.store)
        self.audit = AuditLog(self.store, self.clock, runs_dir=self.runs_dir)
        self.scheduler = Scheduler(self.store, self.clock, self.audit)
        self.runner = tools.ToolRunner(self.store, self.clock, self.audit, self.scheduler)
        self.gate = Gate(self.store, self.clock, self.audit)
        self.approvals = Approvals(self.store, self.clock, self.audit)
        self.memory = Memory(self.store, self.clock, self.audit)
        self.llm = llm or build_client(cassette_dir)
        self.planner = Planner(self.llm)
        self.engine = workflows.WorkflowEngine(
            self.store, self.clock, self.audit, self.runner, self.llm, self.runs_dir
        )

    def initialise(self) -> None:
        self.store.init_from_seed(self.company_dir)
        self.audit.record(
            phase="system", action="world.seeded", actor="system",
            detail={"company_dir": str(self.company_dir), "now": self.clock.iso()},
        )

    def close(self) -> None:
        self.store.close()

    # -- phase 1: detect ---------------------------------------------------

    def detect(self, *, only: str | None = None) -> detect.SweepResult:
        result = detect.sweep(self.store, self.clock, only=only)
        self.audit.record(
            phase="detect", action="sweep.completed", actor="system",
            detail={
                "new": [i.id for i in result.new_items],
                "suppressed": len(result.suppressed),
                "skipped": result.skipped,
                "detectors": sorted(detect.all_detectors()),
            },
        )
        return result

    # -- phases 2 to 4: gather, plan, gate, ask ----------------------------

    def handle(self, item: detect.AttentionItem) -> RunResult:
        """Take an attention item as far as a decision, then stop."""
        principal = self.store.principal(item.subject_user)
        scoped = self.store.scoped(principal)
        run_id = new_id("run")
        folder = RunFolder(self.runs_dir, run_id)

        self.store.conn.execute(
            "insert into runs (id, attention_item_id, principal_id, started_at, status) "
            "values (?, ?, ?, ?, 'running')",
            (run_id, item.id, principal.user_id, self.clock.iso()),
        )
        self.audit.record(
            phase="detect", action="run.started", actor=principal.user_id,
            run_id=run_id, entity="attention_item", entity_id=item.id,
            detail={"detector": item.detector, "summary": item.summary,
                    "dedupe_key": item.dedupe_key, "principal": principal.as_dict()},
        )
        folder.write("01-attention.json", item.as_dict())

        context = self._gather(item, principal, scoped, run_id)
        folder.write("02-context.json", context)

        result = self.planner.plan(
            principal=principal,
            today=self.clock.today().isoformat(),
            attention_item=item.as_dict(),
            context=context,
            tool_catalogue=tools.catalogue(principal.scopes),
            workflow_catalogue=self._workflows_for(principal),
        )
        folder.write("03-prompt.json", result.prompt)
        folder.write("04-plan.json", result.plan.as_dict())
        self.audit.record(
            phase="plan", action="plan.produced", actor=principal.user_id,
            run_id=run_id, entity="plan", entity_id=run_id,
            detail={
                "headline": result.plan.headline,
                "reasoning": result.plan.reasoning,
                "workflow": result.plan.workflow,
                "workflow_params": result.plan.workflow_params,
                "actions": [a.model_dump() for a in result.plan.actions],
                "no_action_reason": result.plan.no_action_reason,
                "citations": [c.model_dump() for c in result.plan.citations],
                "confidence": result.plan.confidence,
                "model": result.call.model,
                "model_call": result.call.key,
                "replayed": result.call.replayed,
                "context_window_chars": result.prompt["size"],
            },
        )

        decision = self.gate.evaluate(
            result.plan, principal=principal, scoped_store=scoped, run_id=run_id
        )
        folder.write("05-gate.json", decision.as_dict())

        if not result.plan.is_write():
            self._finish(run_id, "no_action", result.plan.no_action_reason or "")
            return RunResult(run_id, "no_action", item.as_dict(),
                             result.plan.as_dict(), decision.as_dict(),
                             messages=[result.plan.no_action_reason or ""])

        if not decision.allowed:
            self._finish(run_id, "refused",
                         "; ".join(c.rule for c in decision.failures))
            return RunResult(
                run_id, "refused", item.as_dict(), result.plan.as_dict(),
                decision.as_dict(),
                messages=[f"{c.rule}: {c.message}" for c in decision.failures],
            )

        approval = self.approvals.request(
            run_id=run_id, decision=decision, requested_by=principal.user_id,
            headline=result.plan.headline, plan=result.plan.as_dict(),
        )
        folder.write("06-approval.json", approval)
        self._set_status(run_id, "awaiting_approval")
        return RunResult(run_id, "awaiting_approval", item.as_dict(),
                         result.plan.as_dict(), decision.as_dict(), approval)

    def _gather(self, item, principal: Principal, scoped, run_id: str) -> dict:
        """Ask every provider the principal can use, and record the ones skipped."""
        bundle: dict = {"focus": item.focus, "systems": {}, "skipped_providers": []}
        for name, provider in sorted(providers.all_providers().items()):
            if not provider.usable_by(scoped):
                bundle["skipped_providers"].append(
                    {"provider": name,
                     "reason": "principal holds none of this provider's scopes",
                     "scopes": sorted(provider.scopes)}
                )
                continue
            bundle["systems"][name] = provider.gather(
                scoped, self.clock, item.focus
            ).as_dict()

        bundle["durable_memory"] = self.memory.recall(max_age_days=90)

        self.audit.record(
            phase="context", action="context.gathered", actor=principal.user_id,
            run_id=run_id, entity="attention_item", entity_id=item.id,
            detail={
                "providers_used": sorted(bundle["systems"]),
                "providers_skipped": bundle["skipped_providers"],
                "omitted": {
                    name: result["omitted"]
                    for name, result in bundle["systems"].items() if result["omitted"]
                },
                "record_counts": {
                    name: {k: len(v) if isinstance(v, list) else 1
                           for k, v in result["records"].items()}
                    for name, result in bundle["systems"].items()
                },
                "durable_memory_keys": [m["key"] for m in bundle["durable_memory"]],
            },
        )
        return bundle

    @staticmethod
    def _workflows_for(principal: Principal) -> list[dict]:
        """Only workflows the principal could actually complete.

        A workflow whose steps need a scope this user lacks would fail at its
        first write, after a human had approved it. Better never to offer it.
        """
        return [
            entry for entry in workflows.catalogue()
            if set(entry["required_scopes"]) <= principal.scopes
        ]

    # -- phase 5: execute --------------------------------------------------

    def execute(self, approval_id: str, *, stop_before: str | None = None) -> RunResult:
        """Run an approved plan. Refuses anything not actually approved."""
        approval = self.approvals.get(approval_id)
        run_id = approval["run_id"]
        folder = RunFolder(self.runs_dir, run_id)

        if approval["status"] != "approved":
            self.audit.record(
                phase="execute", action="execute.refused", actor="system",
                run_id=run_id, entity="approval", entity_id=approval_id,
                detail={"status": approval["status"],
                        "reason": "execution requires an approved request"},
            )
            return RunResult(run_id, "not_approved", {},
                             messages=[f"approval {approval_id} is "
                                       f"{approval['status']}"])

        run = self.store.conn.execute(
            "select * from runs where id = ?", (run_id,)
        ).fetchone()
        principal = self.store.principal(run["principal_id"])
        scoped = self.store.scoped(principal)
        plan = Plan.model_validate(approval["payload"]["plan"])
        item_id = run["attention_item_id"]
        item = detect.load_item(self.store, item_id) if item_id else None
        situation = item.dedupe_key if item else run_id

        self.audit.record(
            phase="execute", action="execute.started", actor=principal.user_id,
            run_id=run_id, entity="approval", entity_id=approval_id,
            detail={"approved_by": approval["decided_by"],
                    "path": "workflow" if plan.workflow else "free_form"},
        )

        if plan.workflow:
            execution = self._execute_workflow(
                plan, principal, scoped, run_id, situation, stop_before
            )
        else:
            execution = self._execute_actions(plan, scoped, run_id)

        folder.write("08-execution.json", execution)
        status = execution["status"]
        if status == "completed":
            self._promote_memory(item, plan, principal, run_id)
            if item:
                detect.set_status(self.store, item.id, "handled")
        self._finish(run_id, status, json.dumps(execution.get("summary", {})))
        return RunResult(run_id, status, item.as_dict() if item else {},
                         plan.as_dict(), None, approval, execution)

    def _execute_workflow(self, plan, principal, scoped, run_id, situation,
                          stop_before) -> dict:
        definition = workflows.get(plan.workflow)
        instance_id = self.engine.start(
            definition, plan.workflow_params, run_id=run_id, actor=principal.user_id
        )
        outcome = self.engine.run(
            instance_id, scoped_store=scoped, situation=situation,
            stop_before=stop_before,
        )
        return {
            "path": "workflow",
            "workflow": definition.name,
            "version": definition.version,
            "instance_id": instance_id,
            "status": outcome.status,
            "steps": outcome.steps,
            "compensations": outcome.compensations,
            "error": outcome.error,
        }

    def _execute_actions(self, plan, scoped, run_id) -> dict:
        """The free-form path. Sequential, and it stops at the first failure.

        No compensation here, and the omission is deliberate rather than
        missing. Compensation needs a declared order to unwind; a free-form
        list has no contract about what the earlier actions meant. Work that
        needs unwinding is work that should have been a workflow, and the
        planner is told so. What happens instead is that execution stops and
        the audit log says exactly how far it got.
        """
        results = []
        for index, action in enumerate(plan.actions):
            result = self.runner.invoke(
                action.tool, action.params, scoped_store=scoped, run_id=run_id,
                idempotency_key=f"{run_id}:action:{index}:{action.tool}",
                rationale=action.rationale,
            )
            results.append(result.as_dict() | {"rationale": action.rationale})
            if not result.ok:
                return {"path": "free_form", "status": "failed", "steps": results,
                        "compensations": [],
                        "error": result.error,
                        "stopped_at": index}
        return {"path": "free_form", "status": "completed", "steps": results,
                "compensations": [], "error": None}

    def _promote_memory(self, item, plan: Plan, principal, run_id: str) -> None:
        """Two observations, and nothing the ERP already knows."""
        if item is None:
            return
        focus = item.focus
        if item.detector == "supplier_delay_threatens_production":
            supplier_id = focus.get("supplier_id")
            if supplier_id:
                self.memory.remember(
                    f"supplier.{supplier_id}.slips", "supplier_slip",
                    {"supplier_id": supplier_id, "po_id": focus.get("po_id"),
                     "observed_on": self.clock.today().isoformat(),
                     "part_id": focus.get("part_id")},
                    run_id=run_id, actor=principal.user_id,
                )
        if plan.workflow == "po_reroute":
            part_id = plan.workflow_params.get("part_id")
            if part_id:
                self.memory.remember(
                    f"part.{part_id}.last_reroute", "part_reroute",
                    {"part_id": part_id,
                     "away_from": focus.get("supplier_id"),
                     "prod_order_id": plan.workflow_params.get("prod_order_id"),
                     "on": self.clock.today().isoformat()},
                    run_id=run_id, actor=principal.user_id,
                )

    # -- phase 6: follow up ------------------------------------------------

    def tick(self) -> dict:
        """One heartbeat: reroute stale approvals, then fire what is due.

        In a deployment this is a worker loop. Here it is a CLI command, which
        is the same thing with a slower pulse and a clock you can move.
        """
        rerouted = self.approvals.reroute_stale()
        fired = self.scheduler.run_due(self._handle_due_task)
        return {
            "now": self.clock.iso(),
            "rerouted_approvals": [
                {"approval": r["id"], "now_with": r["requested_of"],
                 "reason": r["routed_reason"]} for r in rerouted
            ],
            "fired_tasks": [
                {"task": f["task"]["id"], "kind": f["task"]["kind"],
                 "outcome": f["outcome"]} for f in fired
            ],
        }

    def _handle_due_task(self, task: dict) -> dict:
        """Deferred work, gated afresh as the user it belongs to."""
        payload = task["payload"]
        subject = payload.get("subject_user")
        if not subject:
            return {"status": "skipped", "reason": "no subject user on the task"}

        principal = self.store.principal(subject)
        scoped = self.store.scoped(principal)

        if task["kind"] == "po_arrival":
            return self._check_po_arrival(task, principal, scoped)
        return {"status": "skipped", "reason": f"no handler for {task['kind']}"}

    def _check_po_arrival(self, task, principal, scoped) -> dict:
        """Did the replacement actually land?

        If it did, say so and stop. If it did not, do two things: re-enter the
        loop from the top with a fresh attention item, and queue another check
        for the next working day. The agent keeps watching until the material
        turns up or a person intervenes, which is what a buyer would do.

        The dedupe key is the order, not the day. A shipment that is late on
        Friday and still late on Tuesday is one unresolved situation, not
        three, so the first miss raises an item and the later ones are
        suppressed against it while still being recorded. Keying on the date
        instead would page somebody every morning about the same late pallet,
        which is how agents get muted.
        """
        context = task["payload"]["context"]
        po_id = context.get("po_id")
        po = scoped.purchase_order(po_id) if po_id else None
        today = self.clock.today()

        if po is None:
            return {"status": "error", "reason": f"purchase order {po_id} is gone"}

        arrived = po.get("status") == "received"
        self.audit.record(
            phase="followup", action="followup.checked", actor=principal.user_id,
            entity="purchase_order", entity_id=po_id,
            detail={"status": po.get("status"), "promised_date": po.get("promised_date"),
                    "arrived": arrived, "checked_on": today.isoformat(),
                    "prod_order_id": context.get("prod_order_id")},
        )
        if arrived:
            return {"status": "arrived", "po_id": po_id, "checked_on": today.isoformat()}

        item = detect.AttentionItem(
            detector="po_arrival_follow_up",
            subject_user=principal.user_id,
            dedupe_key=f"po_missing:{po_id}",
            summary=(
                f"Replacement order {po_id} for {context.get('part_id')} was "
                f"promised {po.get('promised_date')} and is still "
                f"{po.get('status')} on {today.isoformat()}. Production order "
                f"{context.get('prod_order_id')} needs it by "
                f"{context.get('needed_by')}."
            ),
            focus={
                "part_id": context.get("part_id"),
                "po_id": po_id,
                "prod_order_id": context.get("prod_order_id"),
                "supplier_id": po.get("supplier_id"),
                "supplier_emails": [],
                "additional_approvers": [],
            },
            evidence=[
                detect.ref("erp", "purchase_order", po_id,
                           f"{po.get('status')}, promised {po.get('promised_date')}"),
                detect.ref("erp", "production_order", context.get("prod_order_id", ""),
                           f"needs the part by {context.get('needed_by')}"),
                detect.ref("harness", "scheduled_task", task["id"],
                           "arrival check scheduled by the earlier reroute"),
            ],
        )
        item, is_new = detect.persist(self.store, self.clock, item)

        next_check = _next_working_day(today)
        queued = self.scheduler.schedule(
            kind="po_arrival",
            due_at=f"{next_check.isoformat()}T09:00:00",
            payload=task["payload"],
            dedupe_key=f"po_arrival:{next_check.isoformat()}:po_id={po_id}",
            actor=principal.user_id,
        )
        self.audit.record(
            phase="followup", action="followup.reentered", actor="system",
            entity="attention_item", entity_id=item.id,
            detail={"po_id": po_id, "raised_new_item": is_new,
                    "dedupe_key": item.dedupe_key,
                    "next_check": next_check.isoformat(),
                    "next_check_task": queued["id"]},
        )
        return {
            "status": "still_missing",
            "po_id": po_id,
            "checked_on": today.isoformat(),
            "attention_item": item.id,
            "raised_new_item": is_new,
            "next_check": next_check.isoformat(),
        }

    # -- bookkeeping -------------------------------------------------------

    def _set_status(self, run_id: str, status: str) -> None:
        self.store.conn.execute(
            "update runs set status = ? where id = ?", (status, run_id)
        )

    def _finish(self, run_id: str, status: str, outcome: str) -> None:
        self.store.conn.execute(
            "update runs set status = ?, outcome = ?, finished_at = ? where id = ?",
            (status, outcome, self.clock.iso(), run_id),
        )
        self.audit.record(
            phase="system", action="run.finished", actor="system", run_id=run_id,
            entity="run", entity_id=run_id,
            detail={"status": status, "outcome": outcome},
        )
