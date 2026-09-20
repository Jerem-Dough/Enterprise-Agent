"""What a run knows, and what outlives it.

The split is the point, so it is worth stating plainly.

**Run-scoped memory is the run folder.** The attention item, the context
bundle, the prompt, the plan, the gate decision, the step outputs. It lives in
`runs/<run-id>/` and in the `state` dict the workflow engine carries. It is not
in this module because it does not need to be: it is already durable, already
readable, and already scoped to exactly one run by construction.

**Durable memory is this table, and it is deliberately small.** Only two kinds
of fact get promoted out of a run, and both are observations rather than
conclusions:

- `supplier.<id>.slips` — that a supplier moved a promised date, and when.
- `part.<id>.last_reroute` — that a part's supply was rerouted, and to whom.

What is *not* promoted is anything the systems of record already know. The ERP
knows what the current promised date is. Copying it here would create a second
answer to a question that already has one, and the second answer would start
rotting immediately. The rule is: remember what happened, never what is true.
The systems own what is true.

**Every fact carries its age.** `recall` takes a `max_age_days` and drops what
is older, and what it returns carries `observed_at` and `age_days` so the
planner can see how old a belief is rather than receiving it as a timeless
assertion. A memory with no age on it is how an agent ends up confidently
acting on something that stopped being true in March.
"""
from __future__ import annotations

import json
from datetime import datetime

from ..audit import AuditLog
from ..clock import Clock
from ..store import Store

PROMOTABLE = ("supplier_slip", "part_reroute")


class Memory:
    def __init__(self, store: Store, clock: Clock, audit: AuditLog) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit

    def remember(
        self, key: str, kind: str, value: dict, *, run_id: str, actor: str
    ) -> dict:
        """Write a durable observation. Overwrites the same key.

        Overwriting rather than appending is intentional for these two kinds:
        the useful fact is the most recent observation plus a count, not a
        transcript. The transcript already exists in the audit log, which is
        the thing that is allowed to grow without bound.
        """
        if kind not in PROMOTABLE:
            raise ValueError(
                f"{kind!r} is not promotable; durable memory holds only {PROMOTABLE}"
            )
        existing = self.get(key)
        occurrences = int((existing or {}).get("value", {}).get("occurrences", 0)) + 1
        payload = value | {"occurrences": occurrences}

        self._store.conn.execute(
            "insert into memory (key, kind, value, updated_at, source_run) "
            "values (?, ?, ?, ?, ?) "
            "on conflict (key) do update set value = excluded.value, "
            "updated_at = excluded.updated_at, source_run = excluded.source_run",
            (key, kind, json.dumps(payload), self._clock.iso(), run_id),
        )
        self._audit.record(
            phase="memory", action="memory.promoted", actor=actor, run_id=run_id,
            entity="memory", entity_id=key,
            detail={"kind": kind, "value": payload,
                    "replaced": (existing or {}).get("value")},
        )
        return self.get(key)

    def get(self, key: str) -> dict | None:
        row = self._store.conn.execute(
            "select * from memory where key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["value"] = json.loads(record["value"])
        return record

    def recall(self, prefix: str = "", *, max_age_days: int | None = 90) -> list[dict]:
        """Facts under a prefix, aged, with the stale ones dropped."""
        now = self._clock.now()
        out = []
        rows = self._store.conn.execute(
            "select * from memory where key like ? order by key", (f"{prefix}%",)
        )
        for row in rows:
            observed = datetime.fromisoformat(row["updated_at"])
            age = (now - observed).days
            if max_age_days is not None and age > max_age_days:
                continue
            out.append({
                "key": row["key"],
                "kind": row["kind"],
                "value": json.loads(row["value"]),
                "observed_at": row["updated_at"],
                "age_days": age,
                "source_run": row["source_run"],
            })
        return out
