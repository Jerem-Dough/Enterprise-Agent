"""Quality context: lot status and coverage for a lot-tracked part.

Added for Scenario B. It is a separate provider rather than more fields on the
ERP one for a reason worth stating: it is gated by a different scope family
(`erp:quality:*`) held by a different set of users. Folding it into `erp` would
mean every purchasing run attempts a read it will be denied, and the resulting
`omitted` entries would be noise in every audit log rather than signal in the
few where they matter.

Adding it required no change to the kernel, the gate, the planner or the audit
layer. It is a module in this folder and one line in `_load()`.

`coverage` is computed here rather than left to the model because it is
arithmetic with a right answer: does any single released, unallocated lot of
this part hold enough quantity to replace the held one. The model's job is to
choose among the lots that qualify and to say why, not to work out which
qualify.
"""
from __future__ import annotations

from ..clock import Clock
from ..errors import ScopeDenied
from ..store import ScopedStore
from . import ProviderResult, provider


def covering_lots(lots: list[dict], *, part_id: str, needed: float, exclude: str) -> list[dict]:
    """Released, unallocated, same part, big enough. Smallest sufficient first,
    so that the cheapest adequate answer is the first one offered."""
    candidates = [
        lot for lot in lots
        if lot.get("part_id") == part_id
        and lot.get("lot_id") != exclude
        and lot.get("status") == "released"
        and not lot.get("allocated_to")
        and float(lot.get("qty", 0)) >= needed
    ]
    return sorted(candidates, key=lambda lot: float(lot.get("qty", 0)))


@provider(
    "quality",
    description="Lot status, holds, and which released lots could cover an allocation.",
    scopes=["erp:quality:read"],
)
def gather(store: ScopedStore, clock: Clock, focus: dict) -> ProviderResult:
    result = ProviderResult(system="quality")
    part_id = focus.get("part_id")

    try:
        lots = store.quality_lots(part_id=part_id) if part_id else store.quality_lots()
    except ScopeDenied as error:
        result.note_denial(error)
        return result

    result.records["lots"] = lots
    result.records["held_lots"] = [lot for lot in lots if lot.get("status") == "hold"]

    lot_id = focus.get("lot_id")
    needed = float(focus.get("qty_needed") or 0)
    if lot_id and needed:
        result.records["focus_lot"] = next(
            (lot for lot in lots if lot.get("lot_id") == lot_id), None
        )
        eligible = covering_lots(lots, part_id=part_id, needed=needed, exclude=lot_id)
        result.records["qty_needed"] = needed
        result.records["covering_lots"] = eligible
        result.records["coverage_available"] = bool(eligible)
        result.records["rejected_lots"] = [
            {
                "lot_id": lot["lot_id"],
                "reason": (
                    "on hold" if lot.get("status") != "released"
                    else "already allocated" if lot.get("allocated_to")
                    else f"quantity {lot.get('qty')} below the {needed:g} required"
                ),
            }
            for lot in lots
            if lot.get("lot_id") != lot_id and lot not in eligible
        ]
    return result
