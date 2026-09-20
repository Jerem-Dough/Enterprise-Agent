"""The catalogue of things the agent can actually do, and the runner that does them.

A tool is a typed, scoped, idempotent, reversible action. Every one of those
four words is doing work.

**Typed.** Parameters are a Pydantic model, so the same declaration produces the
validation at the door and the JSON schema handed to the model. A model that
hallucinates a field gets a validation error, not a surprising write.

**Scoped.** A tool declares the scopes it needs. The runner checks them before
dispatch and the store checks them again on the way to the data. That is
deliberate duplication: the runner's check produces a clean, auditable refusal
with a rule attached, and the store's check is the one that holds if somebody
later adds a tool and forgets to declare a scope.

**Idempotent.** Every invocation has a key, and the key is recorded with its
result in the same transaction as the write. Calling a tool again with the same
key returns the first result and touches nothing. This is what makes a killed
workflow safe to resume: replaying a step that already ran is a lookup.

**Reversible.** Every tool declares a compensation, and a tool that genuinely
cannot be undone says so in the shape of its compensation rather than by
omitting one. Mail is the honest case: you cannot unsend, so the compensation
sends a correction and says that is what it did. An engine that believed the
step had been erased would be lying to whoever reads the audit log.

To add a tool: drop a module here, declare a params model, decorate the runner
with `@tool(...)`, write its compensation, and add the module to `_load()`.
Nothing in the kernel changes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from ..audit import AuditLog
from ..clock import Clock
from ..errors import ScopeDenied, SiloError
from ..schedule import Scheduler
from ..store import ScopedStore


@dataclass
class ToolContext:
    """Everything a tool is handed.

    Note what is absent: a privileged store. A tool reaches company data only
    through `store`, which is scoped to the principal, and reaches the deferred
    queue only through `scheduler`, which exposes two methods. There is no
    handle here that can read another employee's records.
    """

    store: ScopedStore
    clock: Clock
    scheduler: Scheduler
    run_id: str | None = None


@dataclass
class ToolResult:
    tool: str
    ok: bool
    idempotency_key: str
    replayed: bool
    output: dict
    error: dict | None = None

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
            "output": self.output,
            "error": self.error,
        }


RunFn = Callable[[ToolContext, Any], dict]
CompensateFn = Callable[[ToolContext, Any, dict], dict]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    scopes: frozenset[str]
    params_model: type[BaseModel]
    run: RunFn
    compensate: CompensateFn
    reversible: bool
    compensation_note: str

    def catalogue_entry(self) -> dict:
        """What the planner is shown, and what the README documents."""
        return {
            "name": self.name,
            "description": self.description,
            "required_scopes": sorted(self.scopes),
            "parameters": self.params_model.model_json_schema(),
            "reversible": self.reversible,
            "compensation": self.compensation_note,
        }


REGISTRY: dict[str, Tool] = {}


def tool(
    name: str,
    *,
    description: str,
    scopes: list[str],
    params: type[BaseModel],
    compensate: CompensateFn,
    reversible: bool = True,
    compensation_note: str = "",
):
    def decorate(fn: RunFn) -> RunFn:
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            scopes=frozenset(scopes),
            params_model=params,
            run=fn,
            compensate=compensate,
            reversible=reversible,
            compensation_note=compensation_note,
        )
        return fn

    return decorate


def _load() -> None:
    from . import (  # noqa: F401
        notify,
        purchase_orders,
        quality,
        scheduling,
    )


def all_tools() -> dict[str, Tool]:
    if not REGISTRY:
        _load()
    return dict(REGISTRY)


def get(name: str) -> Tool:
    tools = all_tools()
    if name not in tools:
        raise KeyError(f"no such tool: {name}")
    return tools[name]


def catalogue(principal_scopes: frozenset[str] | None = None) -> list[dict]:
    """The catalogue, optionally narrowed to what a principal could actually run.

    The planner is shown the narrowed list. Proposing an action the user cannot
    take is not a useful recommendation, it is a refusal with extra steps, and
    keeping it out of the context window costs nothing.
    """
    entries = []
    for name, spec in sorted(all_tools().items()):
        if principal_scopes is not None and not spec.scopes <= principal_scopes:
            continue
        entries.append(spec.catalogue_entry())
    return entries


def default_key(run_id: str | None, tool_name: str, params: dict) -> str:
    """A key derived from the call itself, for free-form execution.

    Workflow steps override this with the instance and step id, which is
    stronger: it survives a parameter being recomputed slightly differently on
    resume.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{run_id}|{tool_name}|{canonical}".encode()).hexdigest()
    return f"{tool_name}:{digest[:20]}"


class ToolRunner:
    """The single door through which every write passes."""

    def __init__(self, store, clock: Clock, audit: AuditLog, scheduler: Scheduler) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit
        self._scheduler = scheduler

    def invoke(
        self,
        name: str,
        params: dict,
        *,
        scoped_store: ScopedStore,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        rationale: str = "",
    ) -> ToolResult:
        spec = get(name)
        principal = scoped_store.principal
        key = idempotency_key or default_key(run_id, name, params)

        # 1. Already done? Return what happened last time and touch nothing.
        prior = self._store.conn.execute(
            "select * from tool_invocations where idempotency_key = ?", (key,)
        ).fetchone()
        if prior is not None:
            self._audit.record(
                phase="execute", action="tool.replayed", actor=principal.user_id,
                run_id=run_id, entity="tool", entity_id=name,
                detail={"idempotency_key": key, "originally_invoked_at": prior["invoked_at"]},
            )
            return ToolResult(
                tool=name, ok=True, idempotency_key=key, replayed=True,
                output=json.loads(prior["result"]),
            )

        # 2. Scope check at the door, ahead of the store's own check, so that a
        #    refusal is a decision with a rule attached rather than an exception.
        missing = sorted(spec.scopes - principal.scopes)
        if missing:
            error = ScopeDenied(principal.user_id, missing[0], f"tool:{name}")
            self._audit.record(
                phase="execute", action="tool.denied", actor=principal.user_id,
                run_id=run_id, entity="tool", entity_id=name,
                detail={"missing_scopes": missing, "params": params,
                        "rationale": rationale},
            )
            return ToolResult(
                tool=name, ok=False, idempotency_key=key, replayed=False,
                output={}, error=error.as_dict(),
            )

        # 3. Validate.
        try:
            parsed = spec.params_model.model_validate(params)
        except ValidationError as error:
            detail = {"params": params, "errors": json.loads(error.json())}
            self._audit.record(
                phase="execute", action="tool.invalid_params", actor=principal.user_id,
                run_id=run_id, entity="tool", entity_id=name, detail=detail,
            )
            return ToolResult(
                tool=name, ok=False, idempotency_key=key, replayed=False,
                output={}, error={"error": "ValidationError", "detail": detail},
            )

        # 4. Run. The write, the idempotency record and the audit entry share
        #    one transaction, so there is no state in which a purchase order
        #    exists and the log does not say who made it.
        context = ToolContext(
            store=scoped_store, clock=self._clock, scheduler=self._scheduler, run_id=run_id
        )
        try:
            with self._store.transaction():
                output = spec.run(context, parsed)
                self._store.conn.execute(
                    "insert into tool_invocations "
                    "(idempotency_key, tool, run_id, invoked_at, result) "
                    "values (?, ?, ?, ?, ?)",
                    (key, name, run_id, self._clock.iso(), json.dumps(output, default=str)),
                )
                self._audit.record(
                    phase="execute", action="tool.invoked", actor=principal.user_id,
                    run_id=run_id, entity="tool", entity_id=name,
                    detail={
                        "idempotency_key": key,
                        "params": parsed.model_dump(),
                        "rationale": rationale,
                        "output": output,
                    },
                )
        except SiloError as error:
            self._audit.record(
                phase="execute", action="tool.failed", actor=principal.user_id,
                run_id=run_id, entity="tool", entity_id=name,
                detail={"params": params, "error": error.as_dict()},
            )
            return ToolResult(
                tool=name, ok=False, idempotency_key=key, replayed=False,
                output={}, error=error.as_dict(),
            )
        except Exception as error:  # noqa: BLE001
            self._audit.record(
                phase="execute", action="tool.failed", actor=principal.user_id,
                run_id=run_id, entity="tool", entity_id=name,
                detail={"params": params,
                        "error": {"error": type(error).__name__, "message": str(error)}},
            )
            return ToolResult(
                tool=name, ok=False, idempotency_key=key, replayed=False, output={},
                error={"error": type(error).__name__, "message": str(error)},
            )

        return ToolResult(
            tool=name, ok=True, idempotency_key=key, replayed=False, output=output
        )

    def compensate(
        self,
        name: str,
        params: dict,
        output: dict,
        *,
        scoped_store: ScopedStore,
        run_id: str | None = None,
        reason: str = "",
    ) -> ToolResult:
        """Undo, or record honestly that undoing is not possible.

        The compensation of a step is run with the same principal that ran it.
        Rolling back somebody's action with wider authority than they had would
        be a quiet privilege escalation.
        """
        spec = get(name)
        parsed = spec.params_model.model_validate(params)
        context = ToolContext(
            store=scoped_store, clock=self._clock, scheduler=self._scheduler, run_id=run_id
        )
        try:
            with self._store.transaction():
                result = spec.compensate(context, parsed, output)
                self._audit.record(
                    phase="execute", action="tool.compensated",
                    actor=scoped_store.principal.user_id, run_id=run_id,
                    entity="tool", entity_id=name,
                    detail={"reason": reason, "reversible": spec.reversible,
                            "compensation": result},
                )
        except Exception as error:  # noqa: BLE001
            self._audit.record(
                phase="execute", action="tool.compensation_failed",
                actor=scoped_store.principal.user_id, run_id=run_id,
                entity="tool", entity_id=name,
                detail={"reason": reason,
                        "error": {"error": type(error).__name__, "message": str(error)}},
            )
            return ToolResult(
                tool=name, ok=False, idempotency_key="", replayed=False, output={},
                error={"error": type(error).__name__, "message": str(error)},
            )
        return ToolResult(
            tool=name, ok=True, idempotency_key="", replayed=False, output=result
        )
