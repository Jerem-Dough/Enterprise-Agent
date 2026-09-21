"""ERP context: the part, what is on order for it, and what consumes it.

The shape of what this gathers is driven by the question the scenarios ask:
"is this part going to be short before something needs it?" So it returns the
part's inventory position, every open purchase order against it, every
production order that consumes it, and the supplier records for the suppliers
already involved.

It deliberately does **not** pre-filter candidate suppliers. Deciding which
alternate supplier is eligible is a policy question, it is enforced in the gate
and in the workflow, and a provider that quietly narrowed the list would move
that decision somewhere nobody audits. The provider hands over every supplier
the user may read, including the ones that are wrong, and lets the rules say no
where a reader can see them say it.
"""
from __future__ import annotations

from datetime import timedelta

from ..clock import Clock
from ..errors import ScopeDenied
from ..store import ScopedStore
from . import ProviderResult, provider


def projected_stockout(part: dict, clock: Clock) -> dict:
    """Days of cover at the current usage rate, reported two ways.

    `days_of_cover` nets off safety stock and is what a detector triggers on.
    `days_to_empty` is the raw shelf count a person would quote. Both, because
    they differ, and citing one while the reader thinks of the other reads as
    an error even when the arithmetic is right.
    """
    on_hand = float(part.get("on_hand", 0))
    daily = float(part.get("daily_usage", 0) or 0)
    safety = float(part.get("safety_stock", 0))
    usable = on_hand - safety
    if daily <= 0:
        return {
            "days_of_cover": None,
            "days_to_empty": None,
            "stockout_date": None,
            "usable_on_hand": usable,
            "on_hand": on_hand,
        }
    cover = usable / daily
    empty = on_hand / daily
    return {
        "days_of_cover": round(cover, 2),
        "days_to_empty": round(empty, 2),
        "safety_breach_date": (clock.today() + timedelta(days=int(cover))).isoformat(),
        "stockout_date": (clock.today() + timedelta(days=int(empty))).isoformat(),
        "on_hand": on_hand,
        "usable_on_hand": usable,
        "daily_usage": daily,
        "safety_stock": safety,
    }


@provider(
    "erp",
    description="Parts, inventory position, purchase orders, production orders, suppliers.",
    scopes=["erp:part:read", "erp:po:read", "erp:production:read", "erp:supplier:read"],
)
def gather(store: ScopedStore, clock: Clock, focus: dict) -> ProviderResult:
    result = ProviderResult(system="erp")
    part_id = focus.get("part_id")

    try:
        part = store.part(part_id) if part_id else None
        if part is not None:
            result.records["part"] = part
            result.records["inventory_position"] = projected_stockout(part, clock)
    except ScopeDenied as error:
        result.note_denial(error)

    try:
        orders = store.purchase_orders(part_id=part_id) if part_id else []
        result.records["purchase_orders"] = orders
        result.records["open_purchase_orders"] = [
            o for o in orders if o.get("status") == "open"
        ]
    except ScopeDenied as error:
        result.note_denial(error)

    try:
        consuming = store.production_orders(consumes=part_id) if part_id else []
        result.records["production_orders"] = sorted(
            consuming, key=lambda o: o.get("scheduled_start", "")
        )
    except ScopeDenied as error:
        result.note_denial(error)

    try:
        result.records["suppliers"] = store.suppliers()
    except ScopeDenied as error:
        result.note_denial(error)

    if focus.get("prod_order_id"):
        try:
            order = store.production_order(focus["prod_order_id"])
            if order is not None:
                result.records["focus_production_order"] = order
        except ScopeDenied as error:
            result.note_denial(error)

    return result
