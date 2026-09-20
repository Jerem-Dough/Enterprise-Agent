"""Reallocating a production order from a held lot to a good one.

Scenario B's new tool. It is the cleanest of the five to compensate, because it
is a pure record change inside one system: swap the allocation back and the ERP
is exactly where it started. Nothing left the building.

The eligibility of the replacement lot is checked here as well as in the plan
that proposed it. That looks like duplication and is not. The provider computes
coverage so the model can choose; the gate applies policy; and this final check
is the one that runs at the instant of the write, against whatever the lot's
status is right then. A lot that was released when the plan was drafted and
placed on hold while the approval sat in somebody's inbox must not be allocated
because an earlier snapshot said it was fine.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..errors import PolicyViolation
from . import ToolContext, tool


class ReallocateLotParams(BaseModel):
    """Move a production order's allocation from one lot to another."""

    prod_order_id: str = Field(description="The production order, e.g. 4820.")
    part_id: str = Field(description="The lot-tracked part, e.g. P-1180.")
    from_lot: str = Field(description="The lot being released, e.g. L-2093.")
    to_lot: str = Field(description="The replacement lot, e.g. L-2101.")
    reason: str = Field(min_length=10, description="Why. Recorded in the audit log.")


def _compensate_reallocate(context: ToolContext, params: ReallocateLotParams,
                           output: dict) -> dict:
    context.store.reallocate_lot(
        params.prod_order_id, params.part_id, params.to_lot, params.from_lot
    )
    return {
        "compensated": True,
        "effect_reversed": True,
        "prod_order_id": params.prod_order_id,
        "restored_lot": params.from_lot,
    }


@tool(
    "reallocate_lot",
    description=(
        "Move a production order's allocation of a lot-tracked part from one "
        "lot to another. Use when the allocated lot is on quality hold and a "
        "released lot can cover the requirement."
    ),
    scopes=["erp:quality:reallocate", "erp:quality:read", "erp:production:read"],
    params=ReallocateLotParams,
    compensate=_compensate_reallocate,
    compensation_note="Allocates the original lot back to the production order.",
)
def reallocate_lot(context: ToolContext, params: ReallocateLotParams) -> dict:
    target = context.store.quality_lot(params.to_lot)
    if target is None:
        raise KeyError(f"no such lot: {params.to_lot}")

    # Checked at the moment of the write, not against the plan's snapshot.
    if target.get("status") != "released":
        raise PolicyViolation(
            "quality.substitute_lot_must_be_released",
            f"lot {params.to_lot} is {target.get('status')} and cannot be allocated",
            {"lot_id": params.to_lot, "status": target.get("status")},
        )
    already = [o for o in target.get("allocated_to", []) if o != params.prod_order_id]
    if already:
        raise PolicyViolation(
            "quality.substitute_lot_must_be_unallocated",
            f"lot {params.to_lot} is already allocated to {', '.join(already)}",
            {"lot_id": params.to_lot, "allocated_to": already},
        )

    order = context.store.production_order(params.prod_order_id)
    if order is None:
        raise KeyError(f"no such production order: {params.prod_order_id}")
    needed = next(
        (float(c.get("qty", 0)) for c in order.get("components", [])
         if c.get("part_id") == params.part_id),
        0.0,
    )
    if float(target.get("qty", 0)) < needed:
        raise PolicyViolation(
            "quality.substitute_lot_must_cover",
            f"lot {params.to_lot} holds {target.get('qty')} against {needed:g} required",
            {"lot_id": params.to_lot, "qty": target.get("qty"), "required": needed},
        )

    updated = context.store.reallocate_lot(
        params.prod_order_id, params.part_id, params.from_lot, params.to_lot
    )
    component = next(
        (c for c in updated.get("components", []) if c["part_id"] == params.part_id), {}
    )
    return {
        "prod_order_id": params.prod_order_id,
        "part_id": params.part_id,
        "from_lot": params.from_lot,
        "to_lot": params.to_lot,
        "allocated_lots": component.get("allocated_lots", []),
        "qty_required": needed,
        "qty_available": target.get("qty"),
    }
