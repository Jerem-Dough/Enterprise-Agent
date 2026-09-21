"""Deferred work that survives a restart.

The queue is a table, not a timer, so killing the process loses nothing. In a
deployment the caller is a worker loop; here it is a CLI command, which is the
same thing with a slower pulse and a clock you can move.

Firing is not authorisation. A task carries a `subject_user` and never scopes
and never a token, so the work is gated afresh as that user when it fires. A
follow up scheduled on Wednesday for somebody who lost an entitlement on
Thursday must not run with Wednesday's permissions.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from ..audit import AuditLog
from ..clock import Clock
from ..store import Store, new_id


class Scheduler:
    """The durable queue. Tools receive this rather than a privileged store."""

    def __init__(self, store: Store, clock: Clock, audit: AuditLog) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit

    def schedule(
        self,
        *,
        kind: str,
        due_at: str | datetime,
        payload: dict,
        dedupe_key: str | None = None,
        run_id: str | None = None,
        actor: str = "system",
    ) -> dict:
        """Queue work. Returns the task, new or already present."""
        due_iso = due_at.isoformat() if isinstance(due_at, datetime) else due_at

        if dedupe_key:
            existing = self._store.conn.execute(
                "select * from scheduled_tasks where dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                return dict(existing) | {"created": False}

        task_id = new_id("task")
        self._store.conn.execute(
            "insert into scheduled_tasks "
            "(id, due_at, kind, payload, status, created_at, dedupe_key) "
            "values (?, ?, ?, ?, 'pending', ?, ?)",
            (task_id, due_iso, kind, json.dumps(payload), self._clock.iso(), dedupe_key),
        )
        self._audit.record(
            phase="schedule",
            action="task.scheduled",
            actor=actor,
            run_id=run_id,
            entity="scheduled_task",
            entity_id=task_id,
            detail={"kind": kind, "due_at": due_iso, "payload": payload,
                    "dedupe_key": dedupe_key},
        )
        return {
            "id": task_id, "kind": kind, "due_at": due_iso, "payload": payload,
            "status": "pending", "dedupe_key": dedupe_key, "created": True,
        }

    def cancel(self, task_id: str, *, reason: str = "", actor: str = "system",
               run_id: str | None = None) -> bool:
        """Used as the compensation for a scheduling step."""
        cursor = self._store.conn.execute(
            "update scheduled_tasks set status = 'cancelled' "
            "where id = ? and status = 'pending'",
            (task_id,),
        )
        cancelled = cursor.rowcount > 0
        if cancelled:
            self._audit.record(
                phase="schedule", action="task.cancelled", actor=actor, run_id=run_id,
                entity="scheduled_task", entity_id=task_id, detail={"reason": reason},
            )
        return cancelled

    def pending(self) -> list[dict]:
        return [
            self._row(r) for r in self._store.conn.execute(
                "select * from scheduled_tasks where status = 'pending' order by due_at"
            )
        ]

    def due(self) -> list[dict]:
        now = self._clock.iso()
        return [
            self._row(r) for r in self._store.conn.execute(
                "select * from scheduled_tasks where status = 'pending' and due_at <= ? "
                "order by due_at",
                (now,),
            )
        ]

    def run_due(self, handler: Callable[[dict], dict]) -> list[dict]:
        """Fire everything that has come due, oldest first.

        A task is marked fired before the handler runs. A handler that crashes
        therefore does not leave a task that fires again on the next tick,
        which for work that writes to an ERP is the safer of the two failure
        modes. Retries are the handler's business, and it can schedule one.
        """
        outcomes = []
        for task in self.due():
            self._store.conn.execute(
                "update scheduled_tasks set status = 'fired', fired_at = ? where id = ?",
                (self._clock.iso(), task["id"]),
            )
            self._audit.record(
                phase="schedule", action="task.fired", actor="system",
                entity="scheduled_task", entity_id=task["id"],
                detail={"kind": task["kind"], "due_at": task["due_at"],
                        "payload": task["payload"]},
            )
            outcomes.append({"task": task, "outcome": handler(task)})
        return outcomes

    @staticmethod
    def _row(row) -> dict:
        task = dict(row)
        task["payload"] = json.loads(task["payload"])
        return task
