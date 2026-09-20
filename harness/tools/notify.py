"""Telling people. The honest case for compensation.

A notification cannot be withdrawn. Once production has been told the part is
coming Thursday, the fact that a later step failed does not unsend the message,
and a compensation that reported `{"compensated": true}` and did nothing would
be a lie written into an audit log whose whole value is that it is not one.

So these tools declare `reversible=False` and their compensation sends a
correction referencing the original message. The audit entry records both: that
compensation ran, and that the underlying effect was not erased. Anyone
reconstructing the run learns that a supervisor received a notice that was
later retracted, which is what actually happened.

This is the general rule for any outward-facing effect. Compensation restores
the system of record; it does not restore the world.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import ToolContext, tool


class NotifyProductionParams(BaseModel):
    """Tell the supervisor of a production order about a supply change."""

    prod_order_id: str = Field(description="The production order, e.g. 4812.")
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(
        min_length=20,
        description=(
            "Plain text. State what changed, the new date, and what the "
            "supervisor should do. Do not include information the recipient "
            "cannot verify from the systems named."
        ),
    )


def _compensate_notify(context: ToolContext, params: NotifyProductionParams,
                       output: dict) -> dict:
    original = output.get("message_id")
    recipients = output.get("to", [])
    if not recipients:
        return {"compensated": False, "effect_reversed": False,
                "note": "no message was sent"}
    correction = context.store.send_mail(
        to=recipients,
        subject=f"Retracted: {params.subject}",
        sent_at=context.clock.iso(),
        body=(
            f"Please disregard the previous message about production order "
            f"{params.prod_order_id}. The supply change it described did not "
            f"complete and the order is unchanged. A further update will follow "
            f"once the position is settled."
        ),
    )
    return {
        "compensated": True,
        "effect_reversed": False,
        "note": "a notification cannot be unsent; a correction was sent instead",
        "original_message_id": original,
        "correction_message_id": correction["message_id"],
    }


@tool(
    "notify_production",
    description=(
        "Email the supervisor of a production order about a change that "
        "affects its material supply."
    ),
    scopes=["production:notify", "mail:send", "erp:production:read"],
    params=NotifyProductionParams,
    compensate=_compensate_notify,
    reversible=False,
    compensation_note="Sends a correction. The original notification cannot be unsent.",
)
def notify_production(context: ToolContext, params: NotifyProductionParams) -> dict:
    order = context.store.production_order(params.prod_order_id)
    if order is None:
        raise KeyError(f"no such production order: {params.prod_order_id}")

    supervisor_id = order.get("supervisor_id")
    # The supervisor's address comes from the production order, so notifying is
    # scoped by the record rather than by a free-text recipient the model chose.
    # There is no parameter here that lets a plan mail somebody arbitrary.
    recipient = _supervisor_email(context, supervisor_id)

    message = context.store.send_mail(
        to=[recipient], subject=params.subject, body=params.body,
        sent_at=context.clock.iso(),
    )
    return {
        "message_id": message["message_id"],
        "to": [recipient],
        "supervisor_id": supervisor_id,
        "prod_order_id": params.prod_order_id,
        "subject": params.subject,
    }


class RaiseShortageParams(BaseModel):
    """Hand a material shortage to purchasing.

    Scenario B's fallback. The quality manager holds no purchase order scopes,
    so when no lot can cover an allocation the correct action is not to order
    the part, it is to tell the people who can.
    """

    part_id: str = Field(description="The short part, e.g. P-1180.")
    prod_order_id: str = Field(description="The production order at risk.")
    qty_short: float = Field(gt=0, description="How many units are missing.")
    needed_by: str = Field(description="ISO date the material is required.")
    detail: str = Field(min_length=20, description="Why the shortage exists.")


def _compensate_shortage(context: ToolContext, params: RaiseShortageParams,
                         output: dict) -> dict:
    recipients = output.get("to", [])
    if not recipients:
        return {"compensated": False, "effect_reversed": False}
    correction = context.store.send_mail(
        to=recipients,
        subject=f"Withdrawn: shortage on {params.part_id} for {params.prod_order_id}",
        sent_at=context.clock.iso(),
        body=(
            f"The shortage raised against {params.part_id} for production order "
            f"{params.prod_order_id} has been withdrawn. No purchasing action is "
            f"required."
        ),
    )
    return {
        "compensated": True,
        "effect_reversed": False,
        "note": "the shortage notice cannot be unsent; a withdrawal was sent instead",
        "correction_message_id": correction["message_id"],
    }


@tool(
    "raise_shortage_to_purchasing",
    description=(
        "Tell purchasing that a production order is short of material and no "
        "internal coverage exists. Use when reallocation is not possible."
    ),
    scopes=["mail:send", "erp:production:read", "erp:part:read"],
    params=RaiseShortageParams,
    compensate=_compensate_shortage,
    reversible=False,
    compensation_note="Sends a withdrawal. The original notice cannot be unsent.",
)
def raise_shortage_to_purchasing(context: ToolContext, params: RaiseShortageParams) -> dict:
    recipient = _role_email(context, "Purchasing Manager")
    message = context.store.send_mail(
        to=[recipient],
        subject=f"Shortage: {params.part_id} for production order {params.prod_order_id}",
        sent_at=context.clock.iso(),
        body=(
            f"Production order {params.prod_order_id} is short {params.qty_short:g} of "
            f"{params.part_id}, required by {params.needed_by}.\n\n{params.detail}\n\n"
            f"Raised by {context.store.principal.name} "
            f"({context.store.principal.role})."
        ),
    )
    return {
        "message_id": message["message_id"],
        "to": [recipient],
        "part_id": params.part_id,
        "prod_order_id": params.prod_order_id,
        "qty_short": params.qty_short,
    }


def _supervisor_email(context: ToolContext, user_id: str | None) -> str:
    if not user_id:
        raise ValueError("production order has no supervisor")
    record = context.store.directory().get(user_id)
    if record is None:
        raise ValueError(f"no such colleague: {user_id}")
    return record["email"]


def _role_email(context: ToolContext, role: str) -> str:
    for record in context.store.directory().values():
        if record.get("role") == role:
            return record["email"]
    raise ValueError(f"nobody holds the role {role!r}")
