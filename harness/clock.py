"""The only source of "now" in the harness.

Nothing else calls `datetime.now()`. Every detector, every deadline, every
scheduled task and every date written into a purchase order reads this. That is
what makes the Tuesday follow-up demonstrable: advancing the clock is a normal
operation rather than a test fixture, and because the value lives in the store,
advancing it survives a restart the same way everything else does.

The one deliberate exception is the audit log's `wall_ts`, which records real
elapsed time so that a reader can tell the difference between the world the
agent believed it was in and the moment the entry was actually written.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .store import Store


class Clock:
    """A virtual clock backed by the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def now(self) -> datetime:
        row = self._store.conn.execute("select now from clock where id = 1").fetchone()
        if row is None:
            raise RuntimeError("clock is not initialised; run init_from_seed first")
        return datetime.fromisoformat(row["now"])

    def today(self) -> date:
        return self.now().date()

    def iso(self) -> str:
        return self.now().isoformat()

    def set(self, when: datetime | str) -> datetime:
        if isinstance(when, str):
            when = datetime.fromisoformat(when)
        self._store.conn.execute(
            "insert into clock (id, now) values (1, ?) "
            "on conflict (id) do update set now = excluded.now",
            (when.isoformat(),),
        )
        return when

    def advance(self, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        return self.set(self.now() + timedelta(days=days, hours=hours, minutes=minutes))

    def advance_to(self, when: datetime | str) -> datetime:
        """Move forward to a point in time. Refuses to go backwards, because
        every deferred task in the store was written against a monotonic
        timeline and rewinding would fire work that has already fired."""
        target = datetime.fromisoformat(when) if isinstance(when, str) else when
        current = self.now()
        if target < current:
            raise ValueError(f"cannot rewind the clock from {current} to {target}")
        return self.set(target)

    def end_of_day(self, hour: int) -> datetime:
        return self.now().replace(hour=hour, minute=0, second=0, microsecond=0)

    def is_past_end_of_day(self, hour: int) -> bool:
        return self.now() >= self.end_of_day(hour)


class FrozenClock(Clock):
    """A clock that never moves, for tests that assert on exact timestamps."""

    def __init__(self, store: Store, at: datetime | str) -> None:
        super().__init__(store)
        self._at = datetime.fromisoformat(at) if isinstance(at, str) else at

    def now(self) -> datetime:
        return self._at
