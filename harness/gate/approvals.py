"""Approval requests, decisions, and the backup routing rule.

The rule the brief states: *if an approval request is unanswered at end of day
and the approver's calendar shows them out the next day, it routes to their
designated backup.*

Three things about how that is implemented are deliberate.

**Rerouting is time-driven, not request-driven.** The condition includes "at end
of day", which has not happened when the request is created. So routing to a
backup is a thing that happens to a *pending* request when the clock passes a
threshold, evaluated by `reroute_stale` on every tick. Deciding the backup up
front would answer a question the world has not asked yet.

**Authority does not transfer with the request.** The backup is checked against
the same limits as anyone else. Dana's designated backup holds a lower limit
than Dana, so a request that Dana could have approved may be one that Marcus
cannot, and in that case it escalates rather than lands on a desk that cannot
act on it. An approval routed to somebody without the authority to grant it is
worse than no routing, because it looks like progress.

**A decision is checked, not recorded.** `decide` verifies that the person
answering is the person asked and that their limit covers the value. It is
perfectly possible to build this as a status update and trust the caller. The
whole point of the harness is that the interesting failures come from trusting
a layer that had no reason to be trusted.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ..audit import AuditLog
from ..clock import Clock
from ..store import Store, new_id
from . import GateDecision


class Approvals:
    def __init__(self, store: Store, clock: Clock, audit: AuditLog) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit

    # -- creating ----------------------------------------------------------

    def request(
        self,
        *,
        run_id: str,
        decision: GateDecision,
        requested_by: str,
        headline: str,
        plan: dict,
    ) -> dict:
        policy = self._store.policy().get("approval", {})
        eod_hour = int(policy.get("end_of_day_hour", 17))
        deadline = self._clock.now().replace(
            hour=eod_hour, minute=0, second=0, microsecond=0
        )
        if deadline < self._clock.now():
            deadline += timedelta(days=1)

        approval_id = new_id("apr")
        payload = {
            "headline": headline,
            "plan": plan,
            "gate": decision.as_dict(),
            "value": decision.facts.get("worst_case_value")
            or decision.facts.get("value") or 0,
        }
        self._store.conn.execute(
            "insert into approvals "
            "(id, run_id, requested_at, original_approver, requested_of, "
            " routed_reason, deadline, status, payload) "
            "values (?, ?, ?, ?, ?, NULL, ?, 'pending', ?)",
            (approval_id, run_id, self._clock.iso(), requested_by,
             decision.approver_id, deadline.isoformat(), json.dumps(payload)),
        )
        self._audit.record(
            phase="approval", action="approval.requested", actor=requested_by,
            run_id=run_id, entity="approval", entity_id=approval_id,
            detail={
                "requested_of": decision.approver_id,
                "reason": decision.approval_reason,
                "routing": decision.routing,
                "deadline": deadline.isoformat(),
                "value": payload["value"],
                "headline": headline,
            },
        )
        return self.get(approval_id)

    # -- reading -----------------------------------------------------------

    def get(self, approval_id: str) -> dict:
        row = self._store.conn.execute(
            "select * from approvals where id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such approval: {approval_id}")
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        return record

    def pending(self) -> list[dict]:
        rows = self._store.conn.execute(
            "select * from approvals where status = 'pending' order by requested_at"
        )
        out = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record["payload"])
            out.append(record)
        return out

    # -- deciding ----------------------------------------------------------

    def decide(
        self, approval_id: str, *, verdict: str, by: str, note: str = ""
    ) -> dict:
        if verdict not in ("approved", "rejected"):
            raise ValueError(f"verdict must be approved or rejected, not {verdict!r}")

        record = self.get(approval_id)
        if record["status"] != "pending":
            raise ValueError(
                f"approval {approval_id} is already {record['status']}"
            )

        if by != record["requested_of"]:
            self._audit.record(
                phase="approval", action="approval.refused_decider", actor=by,
                run_id=record["run_id"], entity="approval", entity_id=approval_id,
                detail={"attempted_by": by, "requested_of": record["requested_of"]},
            )
            raise PermissionError(
                f"{approval_id} was asked of {record['requested_of']}, not {by}"
            )

        value = float(record["payload"].get("value") or 0)
        approver = self._store.principal(by)
        if verdict == "approved" and value > 0 and value > approver.po_limit():
            self._audit.record(
                phase="approval", action="approval.refused_authority", actor=by,
                run_id=record["run_id"], entity="approval", entity_id=approval_id,
                detail={"value": value, "approver_limit": approver.po_limit()},
            )
            raise PermissionError(
                f"{approver.name} may approve up to {approver.po_limit():.2f}, "
                f"and this is worth {value:.2f}"
            )

        self._store.conn.execute(
            "update approvals set status = ?, decided_at = ?, decided_by = ?, "
            "note = ? where id = ?",
            (verdict, self._clock.iso(), by, note, approval_id),
        )
        self._audit.record(
            phase="approval", action=f"approval.{verdict}", actor=by,
            run_id=record["run_id"], entity="approval", entity_id=approval_id,
            detail={"note": note, "value": value,
                    "approver_limit": approver.po_limit(),
                    "originally_requested_of": record["original_approver"]},
        )
        return self.get(approval_id)

    # -- the backup rule ---------------------------------------------------

    def reroute_stale(self) -> list[dict]:
        """Move unanswered requests to a backup when the approver is away.

        Run on every tick. The two conditions are checked in the order the rule
        states them: the deadline has passed, and the approver is out the
        following day.
        """
        policy = self._store.policy().get("approval", {})
        if not policy.get("route_to_backup_when_approver_out_next_day", True):
            return []

        now = self._clock.now()
        moved = []

        for record in self.pending():
            deadline = datetime.fromisoformat(record["deadline"])
            if now < deadline:
                continue

            approver_id = record["requested_of"]
            tomorrow = now.date() + timedelta(days=1)
            if not self._out_of_office(approver_id, tomorrow):
                continue

            approver = self._store.principal(approver_id)
            value = float(record["payload"].get("value") or 0)
            target, reason = self._pick_backup(approver, value)

            if target is None:
                self._audit.record(
                    phase="approval", action="approval.reroute_failed",
                    actor="system", run_id=record["run_id"], entity="approval",
                    entity_id=record["id"],
                    detail={"approver": approver_id, "reason": reason,
                            "value": value},
                )
                continue

            self._store.conn.execute(
                "update approvals set requested_of = ?, routed_reason = ? where id = ?",
                (target, reason, record["id"]),
            )
            self._audit.record(
                phase="approval", action="approval.rerouted", actor="system",
                run_id=record["run_id"], entity="approval", entity_id=record["id"],
                detail={
                    "from": approver_id,
                    "to": target,
                    "to_name": self._store.principal(target).name,
                    "reason": reason,
                    "deadline_passed": record["deadline"],
                    "approver_out_on": tomorrow.isoformat(),
                    "value": value,
                },
            )
            moved.append(self.get(record["id"]))
        return moved

    def _pick_backup(self, approver, value: float) -> tuple[str | None, str]:
        """The designated backup, if they can actually approve this.

        If they cannot, the request escalates to the original approver's
        manager instead of sitting with somebody who would have to refuse it.
        """
        backup_id = approver.backup_approver_id
        if backup_id:
            backup = self._store.principal(backup_id)
            if value <= backup.po_limit():
                return backup_id, (
                    f"{approver.name} did not answer by end of day and is out "
                    f"tomorrow; routed to their designated backup {backup.name}"
                )
            if approver.manager_id:
                manager = self._store.principal(approver.manager_id)
                return approver.manager_id, (
                    f"{approver.name} did not answer by end of day and is out "
                    f"tomorrow; their backup {backup.name} may approve up to "
                    f"{backup.po_limit():.2f} and this is worth {value:.2f}, so "
                    f"it went to {manager.name}"
                )
            return None, (
                f"backup {backup.name} lacks authority for {value:.2f} and there "
                f"is no manager to escalate to"
            )
        if approver.manager_id:
            manager = self._store.principal(approver.manager_id)
            return approver.manager_id, (
                f"{approver.name} has no designated backup; routed to their "
                f"manager {manager.name}"
            )
        return None, f"{approver.name} has neither a backup nor a manager"

    def _out_of_office(self, user_id: str, day: date) -> bool:
        target = day.isoformat()
        for event in self._store.documents("calendar_events"):
            if event.get("owner") != user_id or not event.get("out_of_office"):
                continue
            if event.get("start", "")[:10] <= target <= event.get("end", "")[:10]:
                return True
        return False
