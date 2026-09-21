"""The append-only record, and the answer to requirement 7.

Every phase writes here, not just the writes, so a reader holding only this log
can reconstruct what the agent saw, concluded, was allowed to do, who approved,
and what happened in each system.

Two properties make it worth trusting. It cannot be edited: triggers abort any
update or delete for every connection, including the privileged one. And it
cannot be edited quietly: each entry carries the previous entry's hash, so a
removed or altered row breaks verification at a named sequence number.

The SQLite table is authoritative. `runs/<run-id>/audit.jsonl` is a rendering
of it for a human with a text editor, never a second source of truth.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .clock import Clock
from .errors import AuditTampered
from .runlog import RunFolder
from .store import Store

GENESIS = "0" * 64

PHASES = (
    "detect",
    "context",
    "plan",
    "gate",
    "approval",
    "execute",
    "schedule",
    "memory",
    "followup",
    "clock",
    "system",
)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def entry_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(fields)).hexdigest()


class AuditLog:
    """Write here. Do not write anywhere else."""

    def __init__(self, store: Store, clock: Clock, runs_dir: str | None = None) -> None:
        self._store = store
        self._clock = clock
        self._runs_dir = runs_dir

    # -- writing -----------------------------------------------------------

    def record(
        self,
        *,
        phase: str,
        action: str,
        actor: str,
        run_id: str | None = None,
        entity: str | None = None,
        entity_id: str | None = None,
        detail: dict | None = None,
    ) -> dict:
        """Append one entry and return it.

        The sequence number is allocated inside the same statement batch that
        computes the hash, so the chain is well defined even if two runs write
        concurrently. Callers pass domain detail; they never pass hashes.
        """
        if phase not in PHASES:
            raise ValueError(f"unknown audit phase: {phase!r}")

        conn = self._store.conn
        row = conn.execute(
            "select seq, hash from audit_log order by seq desc limit 1"
        ).fetchone()
        seq = (row["seq"] + 1) if row else 1
        prev_hash = row["hash"] if row else GENESIS

        fields = {
            "seq": seq,
            "run_id": run_id,
            "ts": self._clock.iso(),
            "wall_ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "phase": phase,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "detail": detail or {},
        }
        digest = entry_hash(prev_hash, fields)

        conn.execute(
            "insert into audit_log "
            "(seq, run_id, ts, wall_ts, actor, phase, action, entity, entity_id, "
            " detail, prev_hash, hash) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq, run_id, fields["ts"], fields["wall_ts"], actor, phase, action,
                entity, entity_id, json.dumps(fields["detail"], default=str),
                prev_hash, digest,
            ),
        )

        record = fields | {"prev_hash": prev_hash, "hash": digest}
        if run_id and self._runs_dir:
            RunFolder(self._runs_dir, run_id).append_audit(record)
        return record

    # -- reading -----------------------------------------------------------

    def entries(self, run_id: str | None = None) -> list[dict]:
        sql = "select * from audit_log"
        params: tuple = ()
        if run_id is not None:
            sql += " where run_id = ?"
            params = (run_id,)
        sql += " order by seq"
        return [self._row_to_dict(r) for r in self._store.conn.execute(sql, params)]

    @staticmethod
    def _row_to_dict(row) -> dict:
        record = dict(row)
        record["detail"] = json.loads(record["detail"])
        return record

    def verify(self) -> int:
        """Walk the chain. Returns the number of entries verified.

        Raises `AuditTampered` naming the first sequence number that does not
        reproduce, which is what you want during an incident: not "the log is
        bad" but "the log is good up to here."
        """
        prev_hash = GENESIS
        count = 0
        for row in self._store.conn.execute("select * from audit_log order by seq"):
            record = self._row_to_dict(row)
            if record["prev_hash"] != prev_hash:
                raise AuditTampered(
                    f"entry {record['seq']} claims prev_hash {record['prev_hash'][:12]} "
                    f"but the chain is at {prev_hash[:12]}; entries before it verify"
                )
            fields = {
                k: record[k]
                for k in ("seq", "run_id", "ts", "wall_ts", "actor", "phase",
                          "action", "entity", "entity_id", "detail")
            }
            expected = entry_hash(prev_hash, fields)
            if expected != record["hash"]:
                raise AuditTampered(
                    f"entry {record['seq']} does not hash to its recorded value; "
                    f"entries before it verify"
                )
            prev_hash = record["hash"]
            count += 1
        return count

    # -- rendering ---------------------------------------------------------

    def transcript(self, run_id: str) -> str:
        """The run, told in order, for a human who was not there.

        Deliberately built from the log and nothing else. If this reads as a
        gap, the gap is real and the fix is another `record()` call rather than
        a richer renderer.
        """
        lines: list[str] = []
        for record in self.entries(run_id):
            head = (
                f"[{record['ts']}] {record['phase']:<9} {record['action']}"
                f"  actor={record['actor']}"
            )
            if record["entity"]:
                head += f"  {record['entity']}={record['entity_id']}"
            lines.append(head)
            for key, value in record["detail"].items():
                rendered = json.dumps(value, default=str)
                if len(rendered) > 220:
                    rendered = rendered[:217] + "..."
                lines.append(f"{'':>14}{key}: {rendered}")
        return "\n".join(lines)
