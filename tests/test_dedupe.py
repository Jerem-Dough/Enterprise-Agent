"""Trigger dedupe. Required by the brief.

The rule being tested: a dedupe key describes the *situation*, not the alert.
Running the sweep again must produce nothing. A genuinely new development must
produce a new item. Getting this backwards in either direction is a real
failure: too eager and the agent becomes noise somebody mutes, too lazy and it
goes quiet about a problem that actually changed.
"""
from __future__ import annotations

from harness import detect


def test_a_repeated_sweep_produces_nothing_new(harness):
    first = harness.detect()
    assert len(first.new_items) == 2

    second = harness.detect()
    assert second.new_items == []
    assert len(second.suppressed) == 2
    assert {s["dedupe_key"] for s in second.suppressed} == {
        i.dedupe_key for i in first.new_items
    }


def test_suppression_points_at_the_item_that_already_exists(harness):
    first = harness.detect()
    original = {i.dedupe_key: i.id for i in first.new_items}

    second = harness.detect()
    for suppressed in second.suppressed:
        assert suppressed["existing_item"] == original[suppressed["dedupe_key"]]


def test_a_sweep_many_times_over_still_produces_two_items(harness):
    for _ in range(5):
        harness.detect()
    rows = harness.store.conn.execute(
        "select count(*) as n from attention_items"
    ).fetchone()
    assert rows["n"] == 2


def test_a_second_supplier_message_is_a_new_situation(harness):
    """The supplier writing again is genuinely new, and must not be swallowed."""
    harness.detect()

    harness.store.put_document("messages", "M-100", {
        "message_id": "M-100",
        "from": "rita.alvarez@kestrelcomponents.example",
        "to": ["dana.whitfield@northfield-mfg.example"],
        "date": "2026-09-02T11:00:00",
        "subject": "Re: PO-77812, further slip",
        "body": "Now looking at Wednesday 9/9. Apologies again.",
    })
    sweep = harness.detect()

    assert len(sweep.new_items) == 1
    item = sweep.new_items[0]
    assert item.dedupe_key.endswith("M-100")
    assert item.detector == "supplier_delay_threatens_production"


def test_a_hold_lifted_and_replaced_is_a_new_situation(harness):
    """The hold date is in the key, so re-holding a lot raises it again."""
    harness.detect()

    lot = harness.store.document("quality_lots", "L-2093")
    lot["hold_placed_on"] = "2026-09-03"
    harness.store.put_document("quality_lots", "L-2093", lot)
    sweep = harness.detect()

    assert len(sweep.new_items) == 1
    assert sweep.new_items[0].dedupe_key == "lot_hold:L-2093:4820:2026-09-03"


def test_the_bait_email_never_becomes_an_item(harness):
    """Apex writes to Dana about a part they hold an open order for.

    It is a real supplier, a real open order and a real message, and the part
    is nowhere near short. If this ever starts firing, the detector's second
    condition has stopped working.
    """
    sweep = harness.detect()
    focuses = [i.focus for i in sweep.new_items]

    assert not any(f.get("supplier_id") == "S-Q" for f in focuses)
    assert not any(f.get("part_id") == "P-3390" for f in focuses)
    assert "quotes@apexrapid.example" in {
        m["from"] for m in harness.store.documents("messages")
    }, "the bait message is still in the seed"


def test_the_unique_constraint_is_what_enforces_it(harness):
    """Not a read-then-write check, which two concurrent sweeps would race."""
    item = detect.AttentionItem(
        detector="supplier_delay_threatens_production",
        subject_user="u-101",
        dedupe_key="a-key-used-twice",
        summary="First write.",
        focus={"part_id": "P-4471"},
    )
    first, is_new = detect.persist(harness.store, harness.clock, item)
    assert is_new is True

    duplicate = detect.AttentionItem(
        detector="supplier_delay_threatens_production",
        subject_user="u-101",
        dedupe_key="a-key-used-twice",
        summary="Second write, different text, same situation.",
        focus={"part_id": "P-4471"},
    )
    second, is_new = detect.persist(harness.store, harness.clock, duplicate)
    assert is_new is False
    assert second.id == first.id


def test_a_detector_is_skipped_for_a_user_missing_its_scopes(harness):
    """Recorded rather than silently passed over."""
    user = harness.store.document("users", "u-101")
    user["scopes"] = [s for s in user["scopes"] if s != "mail:read"]
    harness.store.put_document("users", "u-101", user)

    sweep = harness.detect()
    skipped = [s for s in sweep.skipped if s["user"] == "u-101"]

    assert skipped, "a user who no longer qualifies should be reported, not ignored"
    assert skipped[0]["required"] == ["mail:read"]
    assert not any(i.subject_user == "u-101" for i in sweep.new_items)


def test_a_follow_up_miss_raises_one_item_across_many_days(harness, monkeypatch):
    """The late shipment is one situation, not one per morning."""
    from conftest import approve_and_execute

    approve_and_execute(harness)
    before = harness.store.conn.execute(
        "select count(*) as n from attention_items"
    ).fetchone()["n"]

    for day in ("2026-09-04", "2026-09-07", "2026-09-08"):
        harness.clock.advance_to(f"{day}T09:00:00")
        harness.tick()

    after = harness.store.conn.execute(
        "select count(*) as n from attention_items"
    ).fetchone()["n"]
    assert after == before + 1, "three misses of one order raised one item"

    missing = harness.store.conn.execute(
        "select dedupe_key from attention_items where detector = 'po_arrival_follow_up'"
    ).fetchall()
    assert len(missing) == 1
    assert missing[0]["dedupe_key"].startswith("po_missing:")
