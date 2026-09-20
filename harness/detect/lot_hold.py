"""Scenario B's trigger: a lot on hold is still allocated to a live order.

Simpler than the Scenario A detector, and worth noticing how much simpler. The
whole condition is visible in the ERP: a lot has been placed on hold, a
production order that starts soon is still counting on it, and nobody has
reallocated. No prose has to be understood, so nothing outside the ERP is
consulted.

This is the shape most detectors should have. The supplier delay case needs a
second system only because the truth arrived by email before it arrived in the
system of record, which is a real and common situation rather than the default
one.

Adding this detector required no change to the sweep, the kernel, the gate or
the audit layer. It is a module in this folder and one name in `_load()`.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from ..clock import Clock
from ..store import ScopedStore
from . import AttentionItem, detector, ref

HORIZON_DAYS = 14


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


@detector(
    "lot_hold_blocks_production",
    description=(
        "A lot on quality hold is still allocated to a production order that "
        "starts inside the horizon."
    ),
    roles=["Quality Manager"],
    scopes=["erp:quality:read", "erp:production:read", "erp:part:read"],
)
def detect(store: ScopedStore, clock: Clock) -> Iterable[AttentionItem]:
    today = clock.today()
    horizon = today + timedelta(days=HORIZON_DAYS)

    for lot in store.quality_lots(status="hold"):
        for prod_order_id in lot.get("allocated_to", []):
            order = store.production_order(prod_order_id)
            if order is None or order.get("status") not in ("planned", "released"):
                continue
            start = _as_date(order.get("scheduled_start"))
            if start is None or not (today <= start <= horizon):
                continue

            needed = next(
                (
                    float(c.get("qty", 0))
                    for c in order.get("components", [])
                    if c.get("part_id") == lot["part_id"]
                ),
                0.0,
            )
            part = store.part(lot["part_id"])

            yield AttentionItem(
                detector="lot_hold_blocks_production",
                subject_user=store.principal.user_id,
                # A hold lifted and placed again is a new situation, so the
                # date the hold was placed belongs in the key.
                dedupe_key=(
                    f"lot_hold:{lot['lot_id']}:{prod_order_id}"
                    f":{lot.get('hold_placed_on')}"
                ),
                summary=(
                    f"Lot {lot['lot_id']} of {lot['part_id']} is on hold "
                    f"({lot.get('hold_reason')}) and is still allocated to "
                    f"production order {prod_order_id}, which starts "
                    f"{order['scheduled_start']} and needs {needed:g}."
                ),
                focus={
                    "part_id": lot["part_id"],
                    "lot_id": lot["lot_id"],
                    "prod_order_id": prod_order_id,
                    "qty_needed": needed,
                    "supervisor_id": order.get("supervisor_id"),
                    "additional_approvers": [],
                },
                evidence=[
                    ref("erp", "quality_lot", lot["lot_id"],
                        f"on hold since {lot.get('hold_placed_on')}: {lot.get('hold_reason')}"),
                    ref("erp", "production_order", prod_order_id,
                        f"starts {order['scheduled_start']}, needs {needed:g}"),
                    ref("erp", "part", lot["part_id"],
                        (part or {}).get("description", "")),
                ],
            )
