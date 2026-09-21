"""Declared workflows: the definition is in charge, not the model.

Purchasing fixed the steps and their order, so the planner's authority stops at
the boundary. It decides whether to enter a workflow and supplies parameters;
everything after that is the definition.

What makes that structural rather than intended:

- `steps` is an immutable tuple walked by index, so nothing can reorder or
  insert a step.
- `Plan` refuses to carry a workflow and free-form actions together, so a model
  cannot append one by putting it elsewhere.
- Idempotency keys are `instance:step`, not a hash of arguments, so a resumed
  step that recomputes a parameter cannot write twice.

State persists after every step; a failure compensates backwards, as the same
principal, and records what it could not undo. See CONTEXT.md.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from ..audit import AuditLog
from ..clock import Clock
from ..errors import SiloError
from ..plan.llm import LLMClient
from ..runlog import RunFolder
from ..store import ScopedStore, Store, new_id
from ..tools import ToolRunner, get as get_tool


class WorkflowKilled(SiloError):
    """Raised by the injected stop point that simulates a crash."""


@dataclass
class StepContext:
    """Everything a step is given."""

    instance_id: str
    run_id: str
    step_id: str
    params: BaseModel
    state: dict
    store: ScopedStore
    clock: Clock
    llm: LLMClient
    runner: ToolRunner
    situation: str

    def idempotency_key(self) -> str:
        """Derived from the instance and the step, not from the arguments.

        This is the stronger form. If a resumed step recomputes a parameter
        even slightly differently, a key derived from arguments would miss and
        the step would run twice. This one cannot.
        """
        return f"{self.instance_id}:{self.step_id}"

    def output_of(self, step_id: str) -> dict:
        return self.state.get(step_id, {})


@dataclass
class StepOutcome:
    ok: bool
    output: dict = field(default_factory=dict)
    error: dict | None = None
    tool_call: dict | None = None
    """Set when the step invoked a tool, so compensation knows what to undo."""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output, "error": self.error}


@dataclass(frozen=True)
class Step:
    id: str
    description: str
    kind: Literal["check", "tool", "model"]
    run: Callable[[StepContext], StepOutcome]
    tool: str | None = None
    """Named for steps that invoke one, so the gate can compute scopes up front."""


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: str
    description: str
    params_model: type[BaseModel]
    steps: tuple[Step, ...]
    gate_facts: Callable[[BaseModel, ScopedStore], dict]
    """Policy-relevant facts derivable from the parameters alone, so that the
    whole workflow can be gated before its first step writes anything."""

    def required_scopes(self) -> frozenset[str]:
        scopes: set[str] = set()
        for step in self.steps:
            if step.tool:
                scopes |= get_tool(step.tool).scopes
        return frozenset(scopes)

    def catalogue_entry(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "parameters": self.params_model.model_json_schema(),
            "required_scopes": sorted(self.required_scopes()),
            "steps": [
                {"id": s.id, "kind": s.kind, "description": s.description,
                 "tool": s.tool}
                for s in self.steps
            ],
        }


REGISTRY: dict[str, WorkflowDefinition] = {}


def register(definition: WorkflowDefinition) -> WorkflowDefinition:
    REGISTRY[definition.name] = definition
    return definition


def _load() -> None:
    from . import po_reroute  # noqa: F401


def all_workflows() -> dict[str, WorkflowDefinition]:
    if not REGISTRY:
        _load()
    return dict(REGISTRY)


def get(name: str) -> WorkflowDefinition:
    workflows = all_workflows()
    if name not in workflows:
        raise KeyError(f"no such workflow: {name}")
    return workflows[name]


def catalogue() -> list[dict]:
    return [wf.catalogue_entry() for wf in sorted(all_workflows().values(),
                                                  key=lambda w: w.name)]


@dataclass
class WorkflowOutcome:
    instance_id: str
    status: Literal["completed", "failed", "interrupted"]
    steps: list[dict]
    compensations: list[dict] = field(default_factory=list)
    error: dict | None = None

    def as_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "status": self.status,
            "steps": self.steps,
            "compensations": self.compensations,
            "error": self.error,
        }


class WorkflowEngine:
    """Starts, runs, and resumes instances. Knows nothing about purchasing."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        audit: AuditLog,
        runner: ToolRunner,
        llm: LLMClient,
        runs_dir: str | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._runner = runner
        self._llm = llm
        self._runs_dir = runs_dir

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        definition: WorkflowDefinition,
        params: dict,
        *,
        run_id: str,
        actor: str,
    ) -> str:
        parsed = definition.params_model.model_validate(params)
        instance_id = new_id("wf")
        now = self._clock.iso()
        self._store.conn.execute(
            "insert into workflow_instances "
            "(id, run_id, definition, version, status, cursor, params, state, "
            " created_at, updated_at) "
            "values (?, ?, ?, ?, 'running', 0, ?, '{}', ?, ?)",
            (instance_id, run_id, definition.name, definition.version,
             json.dumps(parsed.model_dump(mode="json")), now, now),
        )
        self._audit.record(
            phase="execute", action="workflow.started", actor=actor, run_id=run_id,
            entity="workflow", entity_id=instance_id,
            detail={
                "definition": definition.name,
                "version": definition.version,
                "params": parsed.model_dump(mode="json"),
                "declared_steps": [s.id for s in definition.steps],
            },
        )
        return instance_id

    def run(
        self,
        instance_id: str,
        *,
        scoped_store: ScopedStore,
        situation: str = "",
        stop_before: str | None = None,
    ) -> WorkflowOutcome:
        """Walk the definition from the cursor.

        `stop_before` raises after the previous step has been persisted, which
        is how the tests and the demo simulate a process being killed
        mid-workflow. It is an argument rather than a patched module because
        resumption is a behaviour worth exercising on the real path.
        """
        instance = self._load(instance_id)
        definition = get(instance["definition"])

        if instance["version"] != definition.version:
            error = {
                "error": "WorkflowVersionMismatch",
                "message": (
                    f"instance started under {instance['definition']} "
                    f"v{instance['version']}, the definition on disk is "
                    f"v{definition.version}"
                ),
            }
            self._audit.record(
                phase="execute", action="workflow.version_mismatch",
                actor=scoped_store.principal.user_id, run_id=instance["run_id"],
                entity="workflow", entity_id=instance_id, detail=error,
            )
            return WorkflowOutcome(instance_id, "failed", self._steps(instance_id),
                                   error=error)

        params = definition.params_model.model_validate(instance["params"])
        state = dict(instance["state"])
        actor = scoped_store.principal.user_id
        run_id = instance["run_id"]

        for index in range(instance["cursor"], len(definition.steps)):
            step = definition.steps[index]

            if stop_before == step.id:
                self._audit.record(
                    phase="execute", action="workflow.interrupted", actor=actor,
                    run_id=run_id, entity="workflow", entity_id=instance_id,
                    detail={"stopped_before": step.id, "cursor": index,
                            "completed": [s.id for s in definition.steps[:index]]},
                )
                return WorkflowOutcome(instance_id, "interrupted",
                                       self._steps(instance_id))

            context = StepContext(
                instance_id=instance_id, run_id=run_id, step_id=step.id,
                params=params, state=state, store=scoped_store, clock=self._clock,
                llm=self._llm, runner=self._runner, situation=situation,
            )

            try:
                outcome = step.run(context)
            except Exception as error:  # noqa: BLE001
                outcome = StepOutcome(
                    ok=False,
                    error={"error": type(error).__name__, "message": str(error)},
                )

            self._persist_step(instance_id, index, step, outcome, state, run_id, actor)

            if not outcome.ok:
                compensations = self._compensate(
                    definition, instance_id, index, scoped_store, run_id,
                    reason=f"step {step.id} failed",
                )
                self._set_status(instance_id, "failed")
                self._audit.record(
                    phase="execute", action="workflow.failed", actor=actor,
                    run_id=run_id, entity="workflow", entity_id=instance_id,
                    detail={"failed_step": step.id, "error": outcome.error,
                            "compensated_steps": [c["step_id"] for c in compensations]},
                )
                return WorkflowOutcome(instance_id, "failed", self._steps(instance_id),
                                       compensations, outcome.error)

            state[step.id] = outcome.output

        self._set_status(instance_id, "completed")
        self._audit.record(
            phase="execute", action="workflow.completed", actor=actor, run_id=run_id,
            entity="workflow", entity_id=instance_id,
            detail={"definition": definition.name, "version": definition.version,
                    "steps": [s.id for s in definition.steps]},
        )
        return WorkflowOutcome(instance_id, "completed", self._steps(instance_id))

    # -- internals ---------------------------------------------------------

    def _persist_step(self, instance_id, index, step, outcome, state, run_id, actor):
        """One transaction per step. This is what makes resumption exact."""
        now = self._clock.iso()
        if outcome.ok:
            state = dict(state)
            state[step.id] = outcome.output
        with self._store.transaction() as conn:
            conn.execute(
                "insert into workflow_steps "
                "(instance_id, idx, step_id, status, result, error, started_at, "
                " finished_at) values (?, ?, ?, ?, ?, ?, ?, ?) "
                "on conflict (instance_id, idx) do update set "
                "status = excluded.status, result = excluded.result, "
                "error = excluded.error, finished_at = excluded.finished_at",
                (instance_id, index, step.id, "ok" if outcome.ok else "failed",
                 json.dumps({"output": outcome.output, "tool_call": outcome.tool_call},
                            default=str),
                 json.dumps(outcome.error) if outcome.error else None, now, now),
            )
            conn.execute(
                "update workflow_instances set cursor = ?, state = ?, updated_at = ? "
                "where id = ?",
                (index + 1 if outcome.ok else index, json.dumps(state, default=str),
                 now, instance_id),
            )
        self._audit.record(
            phase="execute",
            action="workflow.step_ok" if outcome.ok else "workflow.step_failed",
            actor=actor, run_id=run_id, entity="workflow_step",
            entity_id=f"{instance_id}:{step.id}",
            detail={"index": index, "kind": step.kind, "tool": step.tool,
                    "output": outcome.output, "error": outcome.error},
        )
        if self._runs_dir and run_id:
            RunFolder(self._runs_dir, run_id).write_step(
                index + 1, step.id,
                {"step": step.id, "kind": step.kind, "tool": step.tool,
                 "description": step.description} | outcome.as_dict(),
            )

    def _compensate(self, definition, instance_id, failed_index, scoped_store,
                    run_id, *, reason) -> list[dict]:
        """Undo completed steps in reverse. Each as the principal that ran it."""
        compensations = []
        for index in range(failed_index - 1, -1, -1):
            row = self._store.conn.execute(
                "select * from workflow_steps where instance_id = ? and idx = ?",
                (instance_id, index),
            ).fetchone()
            if row is None or row["status"] != "ok" or row["compensated"]:
                continue
            payload = json.loads(row["result"] or "{}")
            tool_call = payload.get("tool_call")
            step = definition.steps[index]
            if not tool_call:
                # Checks and model steps wrote nothing, so there is nothing to
                # undo. Recorded anyway, so the audit shows the full unwind.
                compensations.append({"step_id": step.id, "tool": None,
                                      "compensated": True, "effect_reversed": True,
                                      "note": "step made no external change"})
                continue
            result = self._runner.compensate(
                tool_call["tool"], tool_call["params"], tool_call["output"],
                scoped_store=scoped_store, run_id=run_id, reason=reason,
            )
            self._store.conn.execute(
                "update workflow_steps set compensated = 1 "
                "where instance_id = ? and idx = ?",
                (instance_id, index),
            )
            compensations.append(
                {"step_id": step.id, "tool": tool_call["tool"], "ok": result.ok}
                | result.output
            )
        return compensations

    def _load(self, instance_id: str) -> dict:
        row = self._store.conn.execute(
            "select * from workflow_instances where id = ?", (instance_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such workflow instance: {instance_id}")
        instance = dict(row)
        instance["params"] = json.loads(instance["params"])
        instance["state"] = json.loads(instance["state"])
        return instance

    def _steps(self, instance_id: str) -> list[dict]:
        rows = self._store.conn.execute(
            "select idx, step_id, status, result, error, compensated "
            "from workflow_steps where instance_id = ? order by idx",
            (instance_id,),
        )
        return [
            {"index": r["idx"], "step_id": r["step_id"], "status": r["status"],
             "compensated": bool(r["compensated"]),
             "output": json.loads(r["result"] or "{}").get("output", {}),
             "error": json.loads(r["error"]) if r["error"] else None}
            for r in rows
        ]

    def _set_status(self, instance_id: str, status: str) -> None:
        self._store.conn.execute(
            "update workflow_instances set status = ?, updated_at = ? where id = ?",
            (status, self._clock.iso(), instance_id),
        )


def tool_step(context: StepContext, tool_name: str, params: dict) -> StepOutcome:
    """Invoke a tool from inside a step, wired for compensation.

    Shared by every tool step so that the idempotency key and the compensation
    payload are built the same way everywhere, rather than being each step
    author's problem.
    """
    result = context.runner.invoke(
        tool_name, params,
        scoped_store=context.store,
        run_id=context.run_id,
        idempotency_key=context.idempotency_key(),
        rationale=f"workflow step {context.step_id}",
    )
    if not result.ok:
        return StepOutcome(ok=False, error=result.error)
    return StepOutcome(
        ok=True,
        output=result.output | {"replayed": result.replayed},
        tool_call={"tool": tool_name, "params": params, "output": result.output},
    )
