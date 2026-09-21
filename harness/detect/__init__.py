"""Noticing, on a schedule, without being asked.

A detector answers one question: is there a situation a particular employee
would want to know about. It does not gather full context, reason, or propose.

Three rules. Detectors run as the employee, never as the scheduler, so they
cannot surface a situation built from records that user may not see. They are
deterministic, because a detector firing on a model's judgement has a false
positive rate nobody can reason about. And dedupe keys describe the
*situation*, not the alert, so a repeated sweep produces one item while a
genuinely new development produces another. See CONTEXT.md.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..clock import Clock
from ..principal import Principal
from ..store import ScopedStore, Store, new_id


@dataclass
class AttentionItem:
    """One thing that may need a person's attention."""

    detector: str
    subject_user: str
    dedupe_key: str
    summary: str
    focus: dict
    evidence: list[dict] = field(default_factory=list)
    id: str = ""
    created_at: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "detector": self.detector,
            "subject_user": self.subject_user,
            "dedupe_key": self.dedupe_key,
            "created_at": self.created_at,
            "summary": self.summary,
            "focus": self.focus,
            "evidence": self.evidence,
        }


def ref(system: str, kind: str, record_id: str, note: str = "") -> dict:
    """An addressable pointer to a record, so every claim can be traced back."""
    return {"system": system, "kind": kind, "id": record_id, "note": note}


DetectFn = Callable[[ScopedStore, Clock], Iterable[AttentionItem]]


@dataclass(frozen=True)
class Detector:
    name: str
    description: str
    roles: frozenset[str]
    scopes: frozenset[str]
    detect: DetectFn

    def applies_to(self, principal: Principal) -> bool:
        """A detector runs for a user only if their role subscribes to it and
        they hold every scope it reads. A user missing a scope is skipped
        rather than partially run, because a half-gathered situation produces a
        confidently wrong alert."""
        return principal.role in self.roles and all(
            principal.has(scope) for scope in self.scopes
        )


REGISTRY: dict[str, Detector] = {}


def detector(name: str, *, description: str, roles: list[str], scopes: list[str]):
    def decorate(fn: DetectFn) -> DetectFn:
        REGISTRY[name] = Detector(
            name=name,
            description=description,
            roles=frozenset(roles),
            scopes=frozenset(scopes),
            detect=fn,
        )
        return fn

    return decorate


def _load() -> None:
    from . import lot_hold, supplier_delay  # noqa: F401


def all_detectors() -> dict[str, Detector]:
    if not REGISTRY:
        _load()
    return dict(REGISTRY)


@dataclass
class SweepResult:
    """What one pass of every detector over every subscribed user produced."""

    new_items: list[AttentionItem] = field(default_factory=list)
    suppressed: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "new_items": [item.as_dict() for item in self.new_items],
            "suppressed": self.suppressed,
            "skipped": self.skipped,
        }


def persist(store: Store, clock: Clock, item: AttentionItem) -> tuple[AttentionItem, bool]:
    """Write an item unless its situation is already open. Returns the item and
    whether it was new.

    The uniqueness constraint on `dedupe_key` is what does the work, so two
    sweeps racing produce one item rather than two, which is not true of a
    read-then-write check.
    """
    existing = store.conn.execute(
        "select id, created_at from attention_items where dedupe_key = ?",
        (item.dedupe_key,),
    ).fetchone()
    if existing is not None:
        item.id = existing["id"]
        item.created_at = existing["created_at"]
        return item, False

    item.id = new_id("ai")
    item.created_at = clock.iso()
    store.conn.execute(
        "insert into attention_items "
        "(id, dedupe_key, detector, subject_user, created_at, status, data) "
        "values (?, ?, ?, ?, ?, 'open', ?)",
        (
            item.id, item.dedupe_key, item.detector, item.subject_user,
            item.created_at, json.dumps(item.as_dict()),
        ),
    )
    return item, True


def sweep(store: Store, clock: Clock, *, only: str | None = None) -> SweepResult:
    """Run every detector for every user whose role subscribes to it.

    Enumeration happens with the privileged handle because listing employees is
    the scheduler's job, not any employee's. Execution then drops to that
    employee's scoped handle immediately.
    """
    result = SweepResult()
    users = [Principal.from_row(row) for row in store.documents("users")]

    for name, det in sorted(all_detectors().items()):
        if only is not None and name != only:
            continue
        for principal in sorted(users, key=lambda p: p.user_id):
            if not det.applies_to(principal):
                if principal.role in det.roles:
                    result.skipped.append(
                        {
                            "detector": name,
                            "user": principal.user_id,
                            "reason": "missing scopes",
                            "required": sorted(det.scopes - principal.scopes),
                        }
                    )
                continue
            for item in det.detect(store.scoped(principal), clock):
                item, is_new = persist(store, clock, item)
                if is_new:
                    result.new_items.append(item)
                else:
                    result.suppressed.append(
                        {
                            "detector": name,
                            "user": principal.user_id,
                            "dedupe_key": item.dedupe_key,
                            "existing_item": item.id,
                            "first_seen": item.created_at,
                        }
                    )
    return result


def load_item(store: Store, item_id: str) -> AttentionItem | None:
    row = store.conn.execute(
        "select data from attention_items where id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"])
    return AttentionItem(**data)


def set_status(store: Store, item_id: str, status: str) -> None:
    store.conn.execute(
        "update attention_items set status = ? where id = ?", (status, item_id)
    )
