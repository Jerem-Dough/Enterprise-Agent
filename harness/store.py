"""Durable state, and the only place a scope check can be skipped by accident.

Two handles exist and the difference between them is the security model.

`Store` is privileged. It owns the connection, the schema, the seed load, and
the harness's own bookkeeping (runs, approvals, workflow state, the schedule,
the audit chain). It performs no scope checks, because the things that use it
are the trusted computing base: the kernel, the gate, the scheduler, the audit.

`ScopedStore` is what providers and tools receive, and it is the only handle
they ever receive. Every method on it names the scope it requires and raises
`ScopeDenied` without it. Reads that belong to a person (mail, calendar) filter
on the principal held by the handle rather than on an argument, so a caller
cannot ask for somebody else's inbox at all. There is no method that widens a
`ScopedStore` back into a `Store`.

The point, borrowed from a system where this was enforced by Postgres RLS: a
forgotten check should return nothing, not everything. A provider that neglects
to think about permissions still cannot read what its principal cannot read,
because the handle it was given cannot express the query.

Why SQLite rather than the JSON files themselves: deferred work and workflow
instances have to survive a restart, tool invocations need an atomic
idempotency ledger, and the audit log needs writes that cannot be taken back.
Files give none of those. The world data lives here too, so that a mutation and
its audit entry commit together or not at all.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Any

from .errors import ScopeDenied
from .principal import Principal

SCHEMA = """
pragma journal_mode = wal;
pragma foreign_keys = on;

-- The modelled world. Documents, not a normalised ERP, because a real
-- connector returns documents and because the scenarios need four entities,
-- not forty tables.
create table if not exists users             (id text primary key, data text not null);
create table if not exists parts             (id text primary key, data text not null);
create table if not exists suppliers         (id text primary key, data text not null);
create table if not exists purchase_orders   (id text primary key, data text not null);
create table if not exists production_orders (id text primary key, data text not null);
create table if not exists quality_lots      (id text primary key, data text not null);
create table if not exists messages          (id text primary key, data text not null);
create table if not exists calendar_events   (id text primary key, data text not null);

-- Harness bookkeeping.
create table if not exists policy (id integer primary key check (id = 1), data text not null);
create table if not exists clock  (id integer primary key check (id = 1), now  text not null);

create table if not exists attention_items (
    id           text primary key,
    dedupe_key   text not null unique,
    detector     text not null,
    subject_user text not null,
    created_at   text not null,
    status       text not null default 'open',
    data         text not null
);

create table if not exists runs (
    id                text primary key,
    attention_item_id text,
    principal_id      text not null,
    started_at        text not null,
    finished_at       text,
    status            text not null,
    outcome           text
);

create table if not exists approvals (
    id                text primary key,
    run_id            text not null,
    requested_at      text not null,
    original_approver text not null,
    requested_of      text not null,
    routed_reason     text,
    deadline          text,
    status            text not null,
    decided_at        text,
    decided_by        text,
    note              text,
    payload           text not null
);

create table if not exists workflow_instances (
    id          text primary key,
    run_id      text not null,
    definition  text not null,
    version     text not null,
    status      text not null,
    cursor      integer not null default 0,
    params      text not null,
    state       text not null,
    created_at  text not null,
    updated_at  text not null
);

create table if not exists workflow_steps (
    instance_id text not null,
    idx         integer not null,
    step_id     text not null,
    status      text not null,
    result      text,
    error       text,
    started_at  text,
    finished_at text,
    compensated integer not null default 0,
    primary key (instance_id, idx)
);

create table if not exists scheduled_tasks (
    id         text primary key,
    due_at     text not null,
    kind       text not null,
    payload    text not null,
    status     text not null default 'pending',
    created_at text not null,
    fired_at   text,
    dedupe_key text unique
);

-- The idempotency ledger. A tool call that has already happened returns its
-- recorded result instead of happening twice.
create table if not exists tool_invocations (
    idempotency_key text primary key,
    tool            text not null,
    run_id          text,
    invoked_at      text not null,
    result          text not null
);

create table if not exists memory (
    key        text primary key,
    kind       text not null,
    value      text not null,
    updated_at text not null,
    source_run text
);

-- Append only, hash chained. Written by harness.audit, never by a tool.
create table if not exists audit_log (
    seq       integer primary key autoincrement,
    run_id    text,
    ts        text not null,
    wall_ts   text not null,
    actor     text not null,
    phase     text not null,
    action    text not null,
    entity    text,
    entity_id text,
    detail    text not null,
    prev_hash text not null,
    hash      text not null
);
create index if not exists audit_log_run on audit_log (run_id, seq);

-- SQLite's answer to revoking update and delete from the application role.
-- These fire for every connection, including the privileged one, so a bug in
-- the harness cannot rewrite history either.
create trigger if not exists audit_log_is_append_only_update
before update on audit_log
begin select raise(ABORT, 'audit_log is append only'); end;

create trigger if not exists audit_log_is_append_only_delete
before delete on audit_log
begin select raise(ABORT, 'audit_log is append only'); end;
"""

_WORLD_TABLES = (
    "users", "parts", "suppliers", "purchase_orders",
    "production_orders", "quality_lots", "messages", "calendar_events",
)

_SEED_FILES = {
    "users": ("users.json", "user_id"),
    "parts": ("erp/parts.json", "part_id"),
    "suppliers": ("erp/suppliers.json", "supplier_id"),
    "purchase_orders": ("erp/purchase_orders.json", "po_id"),
    "production_orders": ("erp/production_orders.json", "prod_order_id"),
    "quality_lots": ("erp/quality_lots.json", "lot_id"),
    "messages": ("mail/messages.json", "message_id"),
    "calendar_events": ("calendar/events.json", "event_id"),
}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def strip_seed_notes(value: Any) -> Any:
    """Drop keys beginning with an underscore. They document the seed for a
    human reading `company/` and are not part of the modelled world."""
    if isinstance(value, dict):
        return {k: strip_seed_notes(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_seed_notes(v) for v in value]
    return value


class Store:
    """The privileged handle. Construct one per process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One unit of work. A tool's write and its audit entry share one, so a
        write can never end up recorded in the world but absent from history."""
        self.conn.execute("begin")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("rollback")
            raise
        self.conn.execute("commit")

    def init_from_seed(self, company_dir: str | Path) -> None:
        """Load `company/` into the store.

        Re-running replaces the world and leaves the audit log alone, because
        the audit log is the one thing that must not be resettable from inside
        the harness.
        """
        root = Path(company_dir)
        with self.transaction() as conn:
            for table in _WORLD_TABLES:
                conn.execute(f"delete from {table}")
            for table, (relpath, id_key) in _SEED_FILES.items():
                rows = strip_seed_notes(
                    json.loads((root / relpath).read_text(encoding="utf-8"))
                )
                conn.executemany(
                    f"insert into {table} (id, data) values (?, ?)",
                    [(row[id_key], json.dumps(row)) for row in rows],
                )
            policy = json.loads((root / "policy.json").read_text(encoding="utf-8"))
            conn.execute(
                "insert into policy (id, data) values (1, ?) "
                "on conflict (id) do update set data = excluded.data",
                (json.dumps(policy),),
            )
            seed_clock = json.loads((root / "clock.json").read_text(encoding="utf-8"))
            conn.execute(
                "insert into clock (id, now) values (1, ?) "
                "on conflict (id) do update set now = excluded.now",
                (seed_clock["now"],),
            )

    def policy(self) -> dict:
        row = self.conn.execute("select data from policy where id = 1").fetchone()
        return json.loads(row["data"]) if row else {}

    def user(self, user_id: str) -> dict | None:
        row = self.conn.execute("select data from users where id = ?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def principal(self, user_id: str) -> Principal:
        data = self.user(user_id)
        if data is None:
            raise KeyError(f"no such user: {user_id}")
        return Principal.from_row(data)

    def documents(self, table: str) -> list[dict]:
        if table not in _WORLD_TABLES:
            raise ValueError(f"not a world table: {table}")
        return [json.loads(r["data"]) for r in self.conn.execute(f"select data from {table}")]

    def document(self, table: str, doc_id: str) -> dict | None:
        if table not in _WORLD_TABLES:
            raise ValueError(f"not a world table: {table}")
        row = self.conn.execute(f"select data from {table} where id = ?", (doc_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def put_document(self, table: str, doc_id: str, doc: dict) -> None:
        if table not in _WORLD_TABLES:
            raise ValueError(f"not a world table: {table}")
        self.conn.execute(
            f"insert into {table} (id, data) values (?, ?) "
            "on conflict (id) do update set data = excluded.data",
            (doc_id, json.dumps(doc)),
        )

    def scoped(self, principal: Principal) -> ScopedStore:
        """The one door from privileged to scoped. It only goes this way."""
        return ScopedStore(self, principal)


def requires(scope: str):
    """Declare the scope a store operation needs.

    The check lives here rather than in the caller, so that the failure mode of
    forgetting to write a check is a denial and not a leak.
    """

    def decorate(fn):
        @wraps(fn)
        def wrapper(self: ScopedStore, *args, **kwargs):
            if not self.principal.has(scope):
                raise ScopeDenied(self.principal.user_id, scope, fn.__name__)
            return fn(self, *args, **kwargs)

        wrapper.required_scope = scope
        return wrapper

    return decorate


class ScopedStore:
    """Everything a provider or a tool is allowed to touch."""

    def __init__(self, store: Store, principal: Principal) -> None:
        self._store = store
        self.principal = principal

    # -- ERP reads ---------------------------------------------------------

    @requires("erp:part:read")
    def parts(self) -> list[dict]:
        return self._store.documents("parts")

    @requires("erp:part:read")
    def part(self, part_id: str) -> dict | None:
        return self._store.document("parts", part_id)

    @requires("erp:supplier:read")
    def suppliers(self) -> list[dict]:
        return self._store.documents("suppliers")

    @requires("erp:supplier:read")
    def supplier(self, supplier_id: str) -> dict | None:
        return self._store.document("suppliers", supplier_id)

    @requires("erp:po:read")
    def purchase_orders(
        self, *, part_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        rows = self._store.documents("purchase_orders")
        if part_id is not None:
            rows = [r for r in rows if r.get("part_id") == part_id]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return rows

    @requires("erp:po:read")
    def purchase_order(self, po_id: str) -> dict | None:
        return self._store.document("purchase_orders", po_id)

    @requires("erp:production:read")
    def production_orders(
        self, *, status: str | None = None, consumes: str | None = None
    ) -> list[dict]:
        rows = self._store.documents("production_orders")
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        if consumes is not None:
            rows = [
                r for r in rows
                if any(c["part_id"] == consumes for c in r.get("components", []))
            ]
        return rows

    @requires("erp:production:read")
    def production_order(self, prod_order_id: str) -> dict | None:
        return self._store.document("production_orders", prod_order_id)

    @requires("erp:quality:read")
    def quality_lots(
        self, *, part_id: str | None = None, status: str | None = None
    ) -> list[dict]:
        rows = self._store.documents("quality_lots")
        if part_id is not None:
            rows = [r for r in rows if r.get("part_id") == part_id]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        return rows

    @requires("erp:quality:read")
    def quality_lot(self, lot_id: str) -> dict | None:
        return self._store.document("quality_lots", lot_id)

    # -- Directory ---------------------------------------------------------

    def directory(self) -> dict[str, dict]:
        """Names, roles and work addresses for every colleague.

        Deliberately ungated. An employee can always look up who a colleague is
        and how to email them, and a harness that pretended otherwise would
        force tools to reach around the scoped handle to send a message, which
        is a far worse outcome than exposing a staff list.

        Equally deliberately, it returns four fields. Scopes, approval limits
        and reporting lines are not in it. Those are authorization facts, they
        belong to the gate, and the gate uses the privileged handle.
        """
        return {
            row["user_id"]: {
                "user_id": row["user_id"],
                "name": row["name"],
                "role": row["role"],
                "email": row["email"],
            }
            for row in self._store.documents("users")
        }

    # -- Mail and calendar -------------------------------------------------
    #
    # These take no user argument on purpose. The handle already knows whose
    # they are, so there is no call a caller could make to reach another
    # person's inbox.

    @requires("mail:read")
    def inbox(self, *, since: str | None = None) -> list[dict]:
        me = self.principal.email
        rows = [
            m for m in self._store.documents("messages")
            if me in m.get("to", []) or m.get("from") == me
        ]
        if since is not None:
            rows = [m for m in rows if m.get("date", "") >= since]
        return sorted(rows, key=lambda m: m.get("date", ""))

    @requires("calendar:read")
    def my_events(self) -> list[dict]:
        me = self.principal.user_id
        return sorted(
            (
                e for e in self._store.documents("calendar_events")
                if e.get("owner") == me or me in e.get("attendees", [])
            ),
            key=lambda e: e.get("start", ""),
        )

    @requires("calendar:read")
    def is_out_of_office(self, user_id: str, day: date) -> bool:
        """Free and busy for any colleague, which is what an organisation
        normally exposes, rather than the contents of their calendar. The
        approval routing rule needs to know that somebody is away. It does not
        need to know where they went."""
        target = day.isoformat()
        for event in self._store.documents("calendar_events"):
            if event.get("owner") != user_id or not event.get("out_of_office"):
                continue
            if event.get("start", "")[:10] <= target <= event.get("end", "")[:10]:
                return True
        return False

    # -- Writes ------------------------------------------------------------
    #
    # Primitives only. Idempotency, compensation and the audit entry belong to
    # the tool that wraps these, not to the store.

    @requires("erp:po:create")
    def insert_purchase_order(self, doc: dict) -> dict:
        self._store.put_document("purchase_orders", doc["po_id"], doc)
        return doc

    @requires("erp:po:cancel")
    def amend_purchase_order(self, po_id: str, changes: dict) -> dict:
        doc = self._store.document("purchase_orders", po_id)
        if doc is None:
            raise KeyError(f"no such purchase order: {po_id}")
        doc.update(changes)
        self._store.put_document("purchase_orders", po_id, doc)
        return doc

    @requires("mail:send")
    def send_mail(self, to: list[str], subject: str, body: str, sent_at: str) -> dict:
        doc = {
            "message_id": new_id("M"),
            "from": self.principal.email,
            "to": to,
            "date": sent_at,
            "subject": subject,
            "body": body,
        }
        self._store.put_document("messages", doc["message_id"], doc)
        return doc

    @requires("erp:quality:reallocate")
    def reallocate_lot(
        self, prod_order_id: str, part_id: str, from_lot: str, to_lot: str
    ) -> dict:
        order = self._store.document("production_orders", prod_order_id)
        if order is None:
            raise KeyError(f"no such production order: {prod_order_id}")
        for component in order.get("components", []):
            if component["part_id"] != part_id:
                continue
            allocated = [
                lot for lot in component.get("allocated_lots", []) if lot != from_lot
            ]
            allocated.append(to_lot)
            component["allocated_lots"] = allocated
        self._store.put_document("production_orders", prod_order_id, order)

        for lot_id, attach in ((from_lot, False), (to_lot, True)):
            lot = self._store.document("quality_lots", lot_id)
            if lot is None:
                continue
            allocated = [o for o in lot.get("allocated_to", []) if o != prod_order_id]
            if attach:
                allocated.append(prod_order_id)
            lot["allocated_to"] = allocated
            self._store.put_document("quality_lots", lot_id, lot)
        return order


def required_scopes() -> dict[str, str]:
    """Every scoped operation and the scope it needs, for the tool catalogue
    and for a test that asserts nothing slipped through undecorated."""
    return {
        name: fn.required_scope
        for name, fn in vars(ScopedStore).items()
        if callable(fn) and hasattr(fn, "required_scope")
    }
