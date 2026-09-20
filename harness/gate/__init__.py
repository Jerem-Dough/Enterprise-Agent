"""The gate: permissions, policy, and who has to say yes.

Nothing the model produced reaches a tool without passing through here, and the
single most important property of this module is what it does *not* read.

**The gate does not trust the plan's account of the world.** It re-reads every
fact it decides on from the store. The plan says the supplier is approved; the
gate looks the supplier up. The plan says the order is worth eighteen thousand;
the gate multiplies the quantity by the price on the supplier record. A gate
that validated the model's assertions against the model's other assertions
would be an elaborate way of agreeing with it.

**Every decision names its rule.** A refusal is a `Check` with a rule id, a
message and the values it decided on, written to the audit log. "Denied" is not
an outcome anybody can act on. "`purchase_orders.supplier_must_be_approved_for_part`
refused S-Q because P-4471 is not in its approved_parts" is.

**Every rule runs.** Checks are not short-circuited on the first failure. When
a plan is refused for three reasons, the log says three, because a human who
fixes the first one should not have to discover the second by trying again.

**Approval is required for writes, separately from authority.** Policy says no
write happens without a human. Authority is a second question: whether *this*
person's limit covers *this* value, or whether it has to go up. The two are
independent and conflating them is how an agent ends up executing something
nobody senior enough ever saw.

The gate runs with the privileged store. It is part of the trusted computing
base, alongside the kernel and the audit log: it has to read approval limits
and reporting lines, which are authorization facts rather than company data,
and which no `ScopedStore` exposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from ..audit import AuditLog
from ..clock import Clock
from ..plan.schema import Plan
from ..principal import Principal
from ..store import ScopedStore, Store
from ..tools import all_tools

Verdict = Literal["pass", "fail", "not_applicable"]


@dataclass
class Check:
    rule: str
    verdict: Verdict
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"rule": self.rule, "verdict": self.verdict,
                "message": self.message, "detail": self.detail}


@dataclass
class GateDecision:
    """The whole decision, in a form the audit log can carry verbatim."""

    allowed: bool
    requires_approval: bool
    approval_reason: str
    approver_id: str | None
    routing: dict
    checks: list[Check]
    required_scopes: list[str]
    facts: dict

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.verdict == "fail"]

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "approval_reason": self.approval_reason,
            "approver_id": self.approver_id,
            "routing": self.routing,
            "required_scopes": self.required_scopes,
            "facts": self.facts,
            "checks": [c.as_dict() for c in self.checks],
            "failed_rules": [c.rule for c in self.failures],
        }


class Gate:
    def __init__(self, store: Store, clock: Clock, audit: AuditLog) -> None:
        self._store = store
        self._clock = clock
        self._audit = audit

    # -- entry point -------------------------------------------------------

    def evaluate(
        self,
        plan: Plan,
        *,
        principal: Principal,
        scoped_store: ScopedStore,
        run_id: str,
    ) -> GateDecision:
        policy = self._store.policy()
        checks: list[Check] = []

        if not plan.is_write():
            decision = GateDecision(
                allowed=True, requires_approval=False,
                approval_reason="the plan proposes no write",
                approver_id=None, routing={}, checks=[], required_scopes=[], facts={},
            )
            self._record(decision, principal, run_id)
            return decision

        required_scopes, facts = self._effects(plan, scoped_store)

        checks.extend(self._check_permissions(plan, principal, required_scopes))
        checks.extend(self._check_purchase_policy(facts, policy, scoped_store))
        checks.extend(self._check_quality_policy(facts, policy, scoped_store))

        allowed = not any(c.verdict == "fail" for c in checks)

        requires_approval = bool(
            policy.get("approval", {}).get("require_approval_for_all_writes", True)
        )
        approver_id, routing, reason = self._route(facts, principal, policy)

        decision = GateDecision(
            allowed=allowed,
            requires_approval=requires_approval and allowed,
            approval_reason=reason if allowed else "the plan was refused",
            approver_id=approver_id if allowed else None,
            routing=routing if allowed else {},
            checks=checks,
            required_scopes=sorted(required_scopes),
            facts=facts,
        )
        self._record(decision, principal, run_id)
        return decision

    # -- what the plan would actually do -----------------------------------

    def _effects(self, plan: Plan, scoped_store: ScopedStore) -> tuple[set[str], dict]:
        """Scopes and policy-relevant facts, for either path.

        For a workflow this comes from the definition, so the whole thing is
        gated before its first step writes anything. That is only possible
        because a definition declares its tools statically and can derive its
        policy facts from its parameters.
        """
        if plan.workflow:
            from .. import workflows

            definition = workflows.get(plan.workflow)
            params = definition.params_model.model_validate(plan.workflow_params)
            facts = definition.gate_facts(params, scoped_store)
            return set(definition.required_scopes()), {
                "path": "workflow",
                "workflow": definition.name,
                "version": definition.version,
                "declared_steps": [s.id for s in definition.steps],
                **facts,
            }

        scopes: set[str] = set()
        catalogue = all_tools()
        actions = []
        for action in plan.actions:
            spec = catalogue.get(action.tool)
            if spec is not None:
                scopes |= spec.scopes
            actions.append({"tool": action.tool, "params": action.params})
        return scopes, {"path": "free_form", "actions": actions}

    # -- permission rules --------------------------------------------------

    def _check_permissions(
        self, plan: Plan, principal: Principal, required: set[str]
    ) -> list[Check]:
        checks = []
        catalogue = all_tools()

        unknown = [a.tool for a in plan.actions if a.tool not in catalogue]
        if plan.actions:
            checks.append(
                Check(
                    "permissions.tool_exists",
                    "fail" if unknown else "pass",
                    f"the plan names tools that do not exist: {unknown}" if unknown
                    else "every named tool is in the catalogue",
                    {"unknown_tools": unknown},
                )
            )

        missing = sorted(required - principal.scopes)
        checks.append(
            Check(
                "permissions.scopes",
                "fail" if missing else "pass",
                f"{principal.user_id} lacks {missing}" if missing
                else f"{principal.user_id} holds every scope this plan needs",
                {"required": sorted(required), "missing": missing},
            )
        )
        return checks

    # -- policy rules ------------------------------------------------------

    def _check_purchase_policy(
        self, facts: dict, policy: dict, store: ScopedStore
    ) -> list[Check]:
        rules = policy.get("purchase_orders", {})
        checks: list[Check] = []

        suppliers = self._suppliers_in_play(facts)
        part_id = facts.get("part_id")

        if rules.get("supplier_must_be_approved_for_part") and suppliers and part_id:
            offending = []
            for supplier_id in suppliers:
                record = store.supplier(supplier_id) or {}
                approved_parts = record.get("approved_parts") or []
                if not record.get("approved") or part_id not in approved_parts:
                    offending.append(
                        {"supplier_id": supplier_id, "name": record.get("name"),
                         "approved": record.get("approved"),
                         "approved_parts": approved_parts}
                    )
            checks.append(
                Check(
                    "purchase_orders.supplier_must_be_approved_for_part",
                    "fail" if offending else "pass",
                    (
                        "refused: "
                        + "; ".join(
                            f"{o['supplier_id']} ({o['name']}) is not approved for "
                            f"{part_id}" for o in offending
                        )
                    ) if offending else
                    f"every supplier in play is approved for {part_id}",
                    {"part_id": part_id, "suppliers_checked": suppliers,
                     "offending": offending},
                )
            )

        premium = rules.get("max_unit_price_premium_pct")
        baseline = float(facts.get("original_unit_price") or 0)
        proposed = float(facts.get("worst_case_unit_price") or 0)
        if premium is not None and baseline > 0 and proposed > 0:
            increase = (proposed - baseline) / baseline * 100
            checks.append(
                Check(
                    "purchase_orders.max_unit_price_premium_pct",
                    "fail" if increase > premium else "pass",
                    f"worst case unit price is {increase:.1f}% above the "
                    f"{baseline:.2f} on the original order, limit {premium:.0f}%",
                    {"baseline_unit_price": baseline, "proposed_unit_price": proposed,
                     "increase_pct": round(increase, 2), "limit_pct": premium},
                )
            )

        needed_by = facts.get("needed_by")
        candidates = facts.get("supplier_candidates") or []
        if needed_by and candidates:
            margin = int(rules.get("min_days_margin_before_production_start", 0))
            deadline = date.fromisoformat(needed_by) - timedelta(days=margin)
            reachable = []
            for supplier_id in candidates:
                record = store.supplier(supplier_id) or {}
                arrival = self._clock.today() + timedelta(
                    days=int(record.get("lead_time_days", 99))
                )
                if arrival <= deadline:
                    reachable.append({"supplier_id": supplier_id,
                                      "arrival": arrival.isoformat()})
            checks.append(
                Check(
                    "purchase_orders.an_approved_supplier_can_make_the_date",
                    "pass" if reachable else "fail",
                    (
                        f"{len(reachable)} of {len(candidates)} approved suppliers "
                        f"can deliver by {needed_by}"
                    ) if reachable else
                    f"no approved supplier can deliver {facts.get('part_id')} by "
                    f"{needed_by}",
                    {"needed_by": needed_by, "required_margin_days": margin,
                     "reachable": reachable, "candidates": candidates},
                )
            )
        return checks

    def _check_quality_policy(
        self, facts: dict, policy: dict, store: ScopedStore
    ) -> list[Check]:
        rules = policy.get("quality", {})
        checks: list[Check] = []
        for action in facts.get("actions", []):
            if action["tool"] != "reallocate_lot":
                continue
            to_lot = action["params"].get("to_lot")
            lot = store.quality_lot(to_lot) if to_lot else None
            if lot is None:
                checks.append(
                    Check("quality.substitute_lot_exists", "fail",
                          f"lot {to_lot} does not exist", {"lot_id": to_lot})
                )
                continue
            if rules.get("substitute_lot_must_be_released"):
                ok = lot.get("status") == "released"
                checks.append(
                    Check("quality.substitute_lot_must_be_released",
                          "pass" if ok else "fail",
                          f"lot {to_lot} is {lot.get('status')}",
                          {"lot_id": to_lot, "status": lot.get("status")})
                )
            if rules.get("substitute_lot_must_be_unallocated"):
                other = [
                    o for o in lot.get("allocated_to", [])
                    if o != action["params"].get("prod_order_id")
                ]
                checks.append(
                    Check("quality.substitute_lot_must_be_unallocated",
                          "pass" if not other else "fail",
                          f"lot {to_lot} is allocated to {other}" if other
                          else f"lot {to_lot} is free",
                          {"lot_id": to_lot, "allocated_to": other})
                )
        return checks

    @staticmethod
    def _suppliers_in_play(facts: dict) -> list[str]:
        """Which suppliers this plan could end up ordering from.

        For a workflow that is the whole candidate list, because the choice has
        not been made yet and approving the plan approves any of them. For
        free-form it is whatever the action names.
        """
        if facts.get("path") == "workflow":
            return list(facts.get("supplier_candidates") or [])
        return [
            a["params"]["supplier_id"]
            for a in facts.get("actions", [])
            if a["tool"] == "create_purchase_order" and a["params"].get("supplier_id")
        ]

    # -- approval routing --------------------------------------------------

    def _route(
        self, facts: dict, principal: Principal, policy: dict
    ) -> tuple[str, dict, str]:
        """Who has to approve this, before any deadline rule applies.

        The subject of the run is the default approver: this is their agent
        acting on their work. Value is the escalation trigger. The backup rule
        is not applied here, because it depends on the request going unanswered,
        which has not happened yet. See `approvals.reroute_stale`.
        """
        value = float(facts.get("worst_case_value") or facts.get("value") or 0)
        limit = principal.po_limit()

        if value > 0 and value > limit:
            escalate_to = policy.get("approval", {}).get("escalate_above_limit_to",
                                                         "manager")
            target = principal.manager_id if escalate_to == "manager" else None
            if target is None:
                return (
                    principal.user_id,
                    {"original_approver": principal.user_id, "escalated": False,
                     "note": "value exceeds the limit and no manager is on record"},
                    f"value {value:.2f} exceeds the {limit:.2f} limit and there is "
                    f"nobody to escalate to",
                )
            manager = self._store.principal(target)
            return (
                target,
                {"original_approver": principal.user_id, "escalated": True,
                 "escalated_to": target, "escalated_to_name": manager.name,
                 "approver_limit": manager.po_limit(), "value": value},
                f"value {value:.2f} exceeds {principal.name}'s {limit:.2f} limit, so "
                f"it goes to {manager.name}",
            )

        return (
            principal.user_id,
            {"original_approver": principal.user_id, "escalated": False,
             "approver_limit": limit, "value": value},
            (
                f"a human must approve every write; value {value:.2f} is within "
                f"{principal.name}'s {limit:.2f} limit"
            ) if value else "a human must approve every write",
        )

    # -- audit -------------------------------------------------------------

    def _record(self, decision: GateDecision, principal: Principal, run_id: str) -> None:
        self._audit.record(
            phase="gate",
            action="gate.allowed" if decision.allowed else "gate.refused",
            actor=principal.user_id,
            run_id=run_id,
            entity="plan",
            entity_id=run_id,
            detail=decision.as_dict(),
        )
