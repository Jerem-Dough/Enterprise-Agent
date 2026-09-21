"""Deferred checks: both kinds the scheduling tool can queue, fired as the user.

The tool's schema offers `po_arrival` and `lot_disposition`. Anything a tool
lets a plan schedule, the kernel has to be able to run, or a follow-up quietly
becomes a row that fires and does nothing.
"""
from __future__ import annotations

from conftest import approve_and_execute


def _queue(harness, kind, subject, context, due="2026-09-04T09:00:00"):
    return harness.scheduler.schedule(
        kind=kind, due_at=due,
        payload={"check": kind, "subject_user": subject, "context": context,
                 "reason": "test", "scheduled_by_run": None},
        dedupe_key=f"test:{kind}:{due}",
    )


def test_an_arrival_check_on_a_received_order_closes_quietly(harness):
    approve_and_execute(harness)
    new_po = next(
        o for o in harness.store.documents("purchase_orders")
        if o["part_id"] == "P-4471" and o["po_id"] != "PO-77812"
    )
    new_po["status"] = "received"
    harness.store.put_document("purchase_orders", new_po["po_id"], new_po)

    harness.clock.advance_to("2026-09-04T09:00:00")
    fired = harness.tick()["fired_tasks"]

    assert [f["outcome"]["status"] for f in fired] == ["arrived"]
    assert not any(
        i["detector"] == "po_arrival_follow_up"
        for i in map(dict, harness.store.conn.execute("select detector from attention_items"))
    )


def test_a_held_lot_raises_one_item_across_repeated_checks(harness):
    _queue(harness, "lot_disposition", "u-202",
           {"lot_id": "L-2093", "part_id": "P-1180", "prod_order_id": "4820"})

    outcomes = []
    for day in ("2026-09-04", "2026-09-07", "2026-09-08"):
        harness.clock.advance_to(f"{day}T09:00:00")
        outcomes += [f["outcome"] for f in harness.tick()["fired_tasks"]]

    assert [o["status"] for o in outcomes] == ["still_held"] * 3
    assert [o["raised_new_item"] for o in outcomes] == [True, False, False]
    assert len({o["attention_item"] for o in outcomes}) == 1

    rows = harness.store.conn.execute(
        "select subject_user, dedupe_key from attention_items "
        "where detector = 'lot_disposition_follow_up'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["subject_user"] == "u-202", "fires as the quality manager"
    assert rows[0]["dedupe_key"] == "lot_undispositioned:L-2093"


def test_a_lot_that_left_hold_resolves_the_check(harness):
    _queue(harness, "lot_disposition", "u-202",
           {"lot_id": "L-2093", "part_id": "P-1180"})
    lot = harness.store.document("quality_lots", "L-2093")
    lot["status"] = "scrapped"
    harness.store.put_document("quality_lots", "L-2093", lot)

    harness.clock.advance_to("2026-09-04T09:00:00")
    outcome = harness.tick()["fired_tasks"][0]["outcome"]

    assert outcome["status"] == "resolved"
    assert outcome["lot_status"] == "scrapped"
    assert harness.scheduler.pending() == [], "nothing re-queued once resolved"


def test_a_check_fires_with_the_users_permissions_not_the_schedulers(harness):
    """Dana cannot read quality lots. A lot check queued for her fails closed."""
    _queue(harness, "lot_disposition", "u-101", {"lot_id": "L-2093"})
    harness.clock.advance_to("2026-09-04T09:00:00")
    outcome = harness.tick()["fired_tasks"][0]["outcome"]

    assert outcome["status"] == "error"
    assert "not readable" in outcome["reason"]


def test_an_unknown_kind_is_skipped_and_says_so(harness):
    _queue(harness, "mystery", "u-101", {})
    harness.clock.advance_to("2026-09-04T09:00:00")
    outcome = harness.tick()["fired_tasks"][0]["outcome"]
    assert outcome["status"] == "skipped"
    assert "mystery" in outcome["reason"]
