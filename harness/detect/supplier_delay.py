"""Scenario A's trigger: a supplier writes about an open PO the line depends on.

The condition is narrower than it looks, and the narrowing is the point. Firing
on "a supplier emailed" is noise. Firing on "a part will run short" never fires
here, because on paper nothing is wrong: the order is promised the 4th and
production starts the 7th. The situation exists only because a supplier said
something in prose that contradicts the ERP.

So it fires on the conjunction: a message from a supplier holding an open PO,
where that PO's part cannot cover the next production order from stock on hand.
Both filters earn their keep against the seed, where two suppliers have written
about parts they supply and only one of those parts is tight.

It deliberately does not read what the email says. Understanding "Monday 9/7,
on your dock Tuesday 9/8" is a language problem, and those belong to the
planner where the reasoning lands in a bundle somebody can read.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from ..clock import Clock
from ..store import ScopedStore
from . import AttentionItem, detector, ref

HORIZON_DAYS = 21


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def covers_from_stock(part: dict, order: dict, today: date) -> dict:
    """Can the part meet this production order without the open PO arriving?

    Stock is drawn down at the daily usage rate between now and the scheduled
    start, and whatever is left has to meet the order's component quantity.
    """
    start = _as_date(order.get("scheduled_start"))
    if start is None:
        return {"covered": True, "reason": "production order has no scheduled start"}

    days_until = max((start - today).days, 0)
    on_hand = float(part.get("on_hand", 0))
    daily = float(part.get("daily_usage", 0) or 0)
    projected = on_hand - daily * days_until
    needed = next(
        (
            float(c.get("qty", 0))
            for c in order.get("components", [])
            if c.get("part_id") == part.get("part_id")
        ),
        0.0,
    )
    return {
        "covered": projected >= needed,
        "days_until_start": days_until,
        "on_hand_today": on_hand,
        "daily_usage": daily,
        "projected_on_hand_at_start": round(projected, 2),
        "required_at_start": needed,
        "shortfall": round(max(needed - projected, 0.0), 2),
    }


@detector(
    "supplier_delay_threatens_production",
    description=(
        "A supplier has written about an open purchase order whose part cannot "
        "cover the next production order that consumes it from stock on hand."
    ),
    roles=["Purchasing Manager", "Operations Director"],
    scopes=[
        "erp:part:read", "erp:po:read", "erp:production:read",
        "erp:supplier:read", "mail:read",
    ],
)
def detect(store: ScopedStore, clock: Clock) -> Iterable[AttentionItem]:
    today = clock.today()
    horizon = today + timedelta(days=HORIZON_DAYS)

    open_pos = store.purchase_orders(status="open")
    if not open_pos:
        return

    suppliers = {s["supplier_id"]: s for s in store.suppliers()}
    messages = store.inbox()

    for po in open_pos:
        supplier = suppliers.get(po.get("supplier_id"))
        if supplier is None:
            continue

        # Correspondence from this supplier since the order was placed.
        ordered_on = po.get("ordered_date", "")
        from_supplier = [
            m for m in messages
            if m.get("from") == supplier.get("contact_email")
            and m.get("date", "")[:10] >= ordered_on
        ]
        if not from_supplier:
            continue

        part = store.part(po["part_id"])
        if part is None:
            continue

        upcoming = [
            o for o in store.production_orders(consumes=po["part_id"])
            if o.get("status") in ("planned", "released")
            and (start := _as_date(o.get("scheduled_start"))) is not None
            and today <= start <= horizon
        ]
        if not upcoming:
            continue

        order = min(upcoming, key=lambda o: o["scheduled_start"])
        coverage = covers_from_stock(part, order, today)
        if coverage["covered"]:
            continue

        latest = max(from_supplier, key=lambda m: m.get("date", ""))
        yield AttentionItem(
            detector="supplier_delay_threatens_production",
            subject_user=store.principal.user_id,
            # The situation is this order, this supplier, and this message. A
            # further message from the supplier is a new situation. Re-running
            # the sweep is not.
            dedupe_key=(
                f"supplier_delay:{po['po_id']}:{order['prod_order_id']}"
                f":{latest['message_id']}"
            ),
            summary=(
                f"{supplier['name']} has written about {po['po_id']} for "
                f"{part['part_id']}. Production order {order['prod_order_id']} "
                f"starts {order['scheduled_start']} and needs "
                f"{coverage['required_at_start']:g}, but stock on hand projects to "
                f"{coverage['projected_on_hand_at_start']:g} by then."
            ),
            focus={
                "part_id": part["part_id"],
                "po_id": po["po_id"],
                "prod_order_id": order["prod_order_id"],
                "supplier_id": supplier["supplier_id"],
                "supplier_emails": [supplier["contact_email"]],
                "additional_approvers": [],
            },
            evidence=[
                ref("erp", "part", part["part_id"], "inventory position"),
                ref("erp", "purchase_order", po["po_id"],
                    f"open, promised {po.get('promised_date')}"),
                ref("erp", "production_order", order["prod_order_id"],
                    f"starts {order['scheduled_start']}"),
                ref("erp", "supplier", supplier["supplier_id"], supplier["name"]),
                ref("mail", "message", latest["message_id"],
                    f"from {latest['from']} on {latest.get('date')}"),
                ref("harness", "coverage_check", order["prod_order_id"],
                    f"shortfall {coverage['shortfall']:g} at scheduled start"),
            ],
        )
