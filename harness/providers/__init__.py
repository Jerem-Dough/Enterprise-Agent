"""Context gathering, one provider per system, scoped to the user.

A provider turns a focus into the records from one system that bear on it. It
never decides anything, and it cannot reach past the `ScopedStore` it is given.

A scope denial is data, not a crash: the provider records it in `omitted` and
returns what it could read, so the plan reasons over a bundle that says plainly
what the user was not allowed to see. Everything returned is addressable by
system, kind and id, so a claim can be traced to the row supporting it.

To add one: a module here, an `@provider` decorator, one line in `_load()`.
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
