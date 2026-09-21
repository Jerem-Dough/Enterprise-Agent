"""Who the agent is acting as, resolved once and carried everywhere.

A `Principal` is the harness's whole notion of identity. It is built once at the
top of a run from the user record and then passed down. Nothing below this line
looks a user up again, which is what makes "scoped to what this user can see" a
property of the call graph rather than a thing each provider remembers to do.

In a real deployment this object is what a token exchange produces. See
docs/DESIGN.md, "Identity and authorization", for how that maps.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    """An authenticated user plus everything authorization needs to decide."""

    user_id: str
    name: str
    email: str
    role: str
    scopes: frozenset[str]
    manager_id: str | None = None
    backup_approver_id: str | None = None
    approval_limits: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, data: dict) -> Principal:
        return cls(
            user_id=data["user_id"],
            name=data["name"],
            email=data["email"],
            role=data["role"],
            scopes=frozenset(data.get("scopes", [])),
            manager_id=data.get("manager_id"),
            backup_approver_id=data.get("backup_approver_id"),
            approval_limits=dict(data.get("approval_limits") or {}),
        )

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def po_limit(self) -> float:
        return float(self.approval_limits.get("po_create_max_value", 0))

    def as_dict(self) -> dict:
        """The form written into the audit log. Scopes are sorted so that two
        audit entries for the same principal hash identically."""
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "manager_id": self.manager_id,
            "backup_approver_id": self.backup_approver_id,
            "approval_limits": self.approval_limits,
        }

    def __str__(self) -> str:
        return f"{self.name} ({self.user_id}, {self.role})"


SYSTEM = Principal(
    user_id="system",
    name="Harmony scheduler",
    email="harmony@northfield-mfg.example",
    role="System",
    scopes=frozenset(),
    approval_limits={},
)
"""The actor for work that happens with no human present.

Detectors and the scheduler run as SYSTEM. It deliberately holds no scopes: it
can notice that something needs attention and it can wake a run up, and it
cannot read a user's mail or write to the ERP. Every read and write in a run is
attributed to the employee the run is for, never to the machinery that started
it. The one privileged path in the system, named and kept narrow rather than
convenient.
"""
