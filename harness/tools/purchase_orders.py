"""Creating and amending purchase orders.

Two tools, and the split between them is a permission boundary rather than a
convenience. Creating an order and cancelling one are different scopes in the
seed (`erp:po:create`, `erp:po:cancel`) because they are different authorities
in real purchasing departments: a buyer may be trusted to place an order
against a contract and not to unwind somebody else's.

That split has a consequence the reroute workflow has to live with. A user who
can create but not cancel gets through the first half of a reroute and is
refused at the second, and the right answer is to compensate the creation
rather than to quietly widen the scope check. The gate catches this earlier by
checking the whole plan's scopes up front, so the refusal arrives before
anything has been written.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..store import new_id
from . import ToolContext, tool


class CreatePurchaseOrderParams(BaseModel):
    """A replacement order. `reason` is required and lands in the audit log."""

    part_id: str = Field(description="The part being ordered, e.g. P-4471.")
    supplier_id: str = Field(description="Supplier to order from, e.g. S-Z.")
    qty: int = Field(gt=0, description="Units to order.")
    unit_price: float = Field(gt=0, description="Agreed price per unit.")
    needed_by: str = Field(
        description="ISO date the material has to be on the dock, e.g. 2026-09-07."
    )
    reason: str = Field(
        min_length=10,
        description="Why this order is being placed. Recorded in the audit log.",
    )


def _compensate_create(context: ToolContext, params: CreatePurchaseOrderParams,
                       output: dict) -> dict:
    """Cancel the order this step created.

    Uses the store's amend primitive directly rather than the cancel tool,
    because compensation is not a new decision and should not be gated a second
    time by a policy that has already approved the forward step.
    """
    po_id = output.get("po_id")
    if not po_id:
        return {"compensated": False, "note": "no purchase order was created"}
    context.store.amend_purchase_order(
        po_id, {"status": "cancelled", "cancelled_reason": "compensating a failed reroute"}
    )
    return {"compensated": True, "po_id": po_id, "new_status": "cancelled"}


@tool(
    "create_purchase_order",
    description=(
        "Place a purchase order with a supplier for a part. Use when existing "
        "supply will not arrive in time and an approved alternate exists."
    ),
    scopes=["erp:po:create", "erp:supplier:read", "erp:part:read"],
    params=CreatePurchaseOrderParams,
    compensate=_compensate_create,
    compensation_note="Cancels the created order.",
)
def create_purchase_order(context: ToolContext, params: CreatePurchaseOrderParams) -> dict:
    supplier = context.store.supplier(params.supplier_id)
    if supplier is None:
        raise KeyError(f"no such supplier: {params.supplier_id}")

    today = context.clock.today()
    lead_time = int(supplier.get("lead_time_days", 0))
    promised = today + timedelta(days=lead_time)

    doc = {
        "po_id": new_id("PO"),
        "part_id": params.part_id,
        "supplier_id": params.supplier_id,
        "qty": params.qty,
        "unit_price": params.unit_price,
        "total_value": round(params.qty * params.unit_price, 2),
        "ordered_date": today.isoformat(),
        "promised_date": promised.isoformat(),
        "status": "open",
        "created_by": context.store.principal.user_id,
        "created_reason": params.reason,
    }
    context.store.insert_purchase_order(doc)
    return doc | {
        "needed_by": params.needed_by,
        "arrives_before_needed": promised <= date.fromisoformat(params.needed_by),
        "supplier_name": supplier.get("name"),
    }


class AmendPurchaseOrderParams(BaseModel):
    """Cancel an order outright, or cut its quantity."""

    po_id: str = Field(description="The order to change, e.g. PO-77812.")
    action: Literal["cancel", "reduce"] = Field(
        description="cancel to void the order, reduce to lower its quantity.",
    )
    new_qty: int | None = Field(
        default=None, ge=0, description="Required when action is reduce."
    )
    reason: str = Field(min_length=10, description="Why. Recorded in the audit log.")


def _compensate_amend(context: ToolContext, params: AmendPurchaseOrderParams,
                      output: dict) -> dict:
    """Put the order back the way it was.

    The forward step records the fields it changed before changing them, which
    is what makes this possible at all. A compensation that reconstructs prior
    state by inference rather than by record is a guess.
    """
    before = output.get("before") or {}
    if not before:
        return {"compensated": False, "note": "no prior state was recorded"}
    context.store.amend_purchase_order(params.po_id, before)
    return {"compensated": True, "po_id": params.po_id, "restored": before}


@tool(
    "amend_purchase_order",
    description=(
        "Cancel an open purchase order, or reduce its quantity. Use on the "
        "original order once replacement supply has been secured."
    ),
    scopes=["erp:po:cancel", "erp:po:read"],
    params=AmendPurchaseOrderParams,
    compensate=_compensate_amend,
    compensation_note="Restores the status and quantity recorded before the change.",
)
def amend_purchase_order(context: ToolContext, params: AmendPurchaseOrderParams) -> dict:
    current = context.store.purchase_order(params.po_id)
    if current is None:
        raise KeyError(f"no such purchase order: {params.po_id}")

    before = {"status": current.get("status"), "qty": current.get("qty"),
              "total_value": current.get("total_value")}

    if params.action == "cancel":
        changes = {"status": "cancelled", "cancelled_reason": params.reason}
    else:
        if params.new_qty is None:
            raise ValueError("new_qty is required when action is reduce")
        changes = {
            "qty": params.new_qty,
            "total_value": round(params.new_qty * float(current.get("unit_price", 0)), 2),
            "reduced_reason": params.reason,
        }

    after = context.store.amend_purchase_order(params.po_id, changes)
    return {
        "po_id": params.po_id,
        "action": params.action,
        "before": before,
        "after": {"status": after.get("status"), "qty": after.get("qty"),
                  "total_value": after.get("total_value")},
    }
