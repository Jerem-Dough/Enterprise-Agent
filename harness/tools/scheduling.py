"""Scheduling a follow up, as a tool rather than a kernel side effect.

The reroute workflow's last step is "schedule the arrival check", so the action
has to be something the workflow can name, whose result is recorded, and whose
compensation cancels it if a later step fails. That makes it a tool.

It requires no scopes, and the reason is worth stating plainly rather than
leaving as an omission. Scheduling a re-check reads nothing and writes nothing
in any company system. It puts a row in the harness's own queue. The work it
eventually wakes up is gated then, as the user, against the permissions they
hold at that moment. Granting authority now for work that runs next Tuesday is
precisely the mistake this design is meant to avoid: deferred work carries a
user id, never a capability.
"""
from __future__ import annotations

from datetime import datetime, time

from pydantic import BaseModel, Field

from . import ToolContext, tool


class ScheduleFollowUpParams(BaseModel):
    """Come back and check that something actually happened."""

    check: str = Field(
        pattern="^(po_arrival|lot_disposition)$",
        description="Which follow up routine to run when this fires.",
    )
    due_date: str = Field(description="ISO date to run the check, e.g. 2026-09-08.")
    subject_user: str = Field(
        description="The user the follow up runs as. Permissions are resolved then, not now."
    )
    context: dict = Field(
        default_factory=dict,
        description="Identifiers the check needs, e.g. po_id and prod_order_id.",
    )
    reason: str = Field(min_length=10, description="Why this check is being scheduled.")


def _compensate_schedule(context: ToolContext, params: ScheduleFollowUpParams,
                         output: dict) -> dict:
    task_id = output.get("task_id")
    if not task_id:
        return {"compensated": False, "note": "no task was scheduled"}
    cancelled = context.scheduler.cancel(
        task_id, reason="compensating a failed workflow", run_id=context.run_id
    )
    return {"compensated": cancelled, "task_id": task_id}


@tool(
    "schedule_follow_up",
    description=(
        "Queue a check to run on a future date, for example confirming that a "
        "replacement shipment actually arrived. Survives a restart."
    ),
    scopes=[],
    params=ScheduleFollowUpParams,
    compensate=_compensate_schedule,
    compensation_note="Cancels the queued task if it has not fired.",
)
def schedule_follow_up(context: ToolContext, params: ScheduleFollowUpParams) -> dict:
    # Due at the start of the working day, not at midnight, so that a check for
    # "did it arrive Tuesday" runs when a dock would plausibly have received it.
    due_at = datetime.combine(datetime.fromisoformat(params.due_date).date(), time(9, 0))

    # The dedupe key is the check and its subject, so a workflow that resumes
    # and replays this step reuses the task rather than queueing a second one.
    identifiers = ":".join(f"{k}={v}" for k, v in sorted(params.context.items()))
    task = context.scheduler.schedule(
        kind=params.check,
        due_at=due_at,
        payload={
            "check": params.check,
            "subject_user": params.subject_user,
            "context": params.context,
            "reason": params.reason,
            "scheduled_by_run": context.run_id,
        },
        dedupe_key=f"{params.check}:{params.due_date}:{identifiers}",
        run_id=context.run_id,
        actor=context.store.principal.user_id,
    )
    return {
        "task_id": task["id"],
        "kind": task["kind"],
        "due_at": task["due_at"],
        "already_scheduled": not task.get("created", True),
        "subject_user": params.subject_user,
    }
