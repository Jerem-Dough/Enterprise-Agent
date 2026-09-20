"""Context gathering, one provider per system, scoped to the user.

A provider turns a focus (a part, a production order, a lot) into the records
from one system that bear on it. It never decides anything. It is handed a
`ScopedStore` and cannot reach past it, so "scoped to what this user can see"
is a property of the handle rather than a rule each provider has to remember.

Three behaviours matter more than the gathering itself.

**A denial is data, not a crash.** If the principal lacks a scope the provider
needs, the provider records the denial in `omitted` and returns what it could
read. The plan then reasons over a context bundle that says plainly what the
user was not allowed to see, and the audit log shows the same. An agent that
silently gathers less is worse than one that says what it missed.

**Providers declare their scopes.** The kernel skips a provider the principal
cannot use at all, and audits the skip, instead of running it to collect a pile
of denials.

**Everything returned is addressable.** Each record carries the system, kind,
and id it came from, so a claim in the model's recommendation can be traced to
the row that supports it. Nothing in the bundle is prose.

To add a provider: drop a module in this folder, decorate a function with
`@provider(...)`, and import it in `_load()` below. Nothing else changes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..clock import Clock
from ..errors import ScopeDenied
from ..store import ScopedStore


@dataclass
class ProviderResult:
    """What one system had to say about the focus."""

    system: str
    records: dict[str, Any] = field(default_factory=dict)
    omitted: list[dict] = field(default_factory=list)

    def note_denial(self, error: ScopeDenied) -> None:
        self.omitted.append(
            {
                "reason": "scope_denied",
                "required_scope": error.required,
                "operation": error.operation,
            }
        )

    def as_dict(self) -> dict:
        return {"system": self.system, "records": self.records, "omitted": self.omitted}


GatherFn = Callable[[ScopedStore, Clock, dict], ProviderResult]


@dataclass(frozen=True)
class Provider:
    name: str
    description: str
    scopes: frozenset[str]
    gather: GatherFn

    def usable_by(self, store: ScopedStore) -> bool:
        """True if the principal holds at least one of this provider's scopes.

        At least one rather than all: a purchasing manager who can read parts
        but not quality lots should still get the part records, with the lot
        read recorded as omitted.
        """
        return any(store.principal.has(scope) for scope in self.scopes)


REGISTRY: dict[str, Provider] = {}


def provider(name: str, *, description: str, scopes: list[str]):
    def decorate(fn: GatherFn) -> GatherFn:
        REGISTRY[name] = Provider(
            name=name, description=description, scopes=frozenset(scopes), gather=fn
        )
        return fn

    return decorate


def _load() -> None:
    from . import calendar, erp, mail, quality  # noqa: F401


def all_providers() -> dict[str, Provider]:
    if not REGISTRY:
        _load()
    return dict(REGISTRY)


def get(name: str) -> Provider:
    return all_providers()[name]
