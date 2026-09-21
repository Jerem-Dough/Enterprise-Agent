"""The gate: permissions, policy, and who has to say yes.

Nothing the model produced reaches a tool without passing through here. The
important property is what the gate does *not* read: it re-derives every fact
it decides on from the store rather than trusting the plan's account of the
world. A gate that checked the model's assertions against the model's other
assertions would be an elaborate way of agreeing with it.

Every rule runs, no short-circuiting, and every decision names its rule with
the values it decided on. "Denied" is not something a person can act on.

Approval requirement and approval authority are separate questions: policy says
no write happens without a human, authority says whether this person's limit
covers this value. See CONTEXT.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from pydantic import ValidationError

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

        checks.extend(self._check_plan_is_runnable(facts))
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

            try:
                definition = workflows.get(plan.workflow)
            except KeyError:
                return set(), {"path": "workflow", "workflow": plan.workflow,
                               "unknown_workflow": True}
            try:
                params = definition.params_model.model_validate(plan.workflow_params)
            except ValidationError as error:
                # A plan naming a workflow it cannot supply parameters for is a
                # refusal with a rule attached, not an exception. The gate is
                # the layer that turns bad model output into a decision, and a
                # stack trace here would take the run down instead.
                return set(definition.required_scopes()), {
                    "path": "workflow",
                    "workflow": definition.name,
                    "version": definition.version,
                    "invalid_parameters": json.loads(error.json()),
                    "supplied": plan.workflow_params,
                }
            facts = definition.gate_facts(params, scoped_store)
            # Every candidate the workflow could land on is an intent, because
            # approval is granted before the choice is made.
            intents = [
                {
                    "supplier_id": supplier_id,
                    "part_id": facts.get("part_id"),
                    "qty": facts.get("qty"),
                    "unit_price": float(
                        (scoped_store.supplier(supplier_id) or {})
                        .get("pricing", {})
                        .get(facts.get("part_id"), 0)
                    ),
                    "needed_by": facts.get("needed_by"),
                }
                for supplier_id in facts.get("supplier_candidates") or []
            ]
            return set(definition.required_scopes()), {
                "path": "workflow",
                "workflow": definition.name,
                "version": definition.version,
                "declared_steps": [s.id for s in definition.steps],
                "purchase_intents": intents,
                **facts,
            }

        scopes: set[str] = set()
        catalogue = all_tools()
        actions = []
        intents = []
        for action in plan.actions:
            spec = catalogue.get(action.tool)
            if spec is not None:
                scopes |= spec.scopes
            actions.append({"tool": action.tool, "params": action.params})
            if action.tool == "create_purchase_order":
                params = action.params
                intents.append({
                    "supplier_id": params.get("supplier_id"),
                    "part_id": params.get("part_id"),
                    "qty": params.get("qty"),
                    "unit_price": float(params.get("unit_price") or 0),
                    "needed_by": params.get("needed_by"),
                })

        # A free-form purchase has to be valued and priced against the part's
        # standing cost, or it would escape both the approval threshold and the
        # price premium rule. Those rules are not workflow features.
        value = sum(
            float(i["unit_price"] or 0) * float(i["qty"] or 0) for i in intents
        )
        baseline = 0.0
        if intents:
            part = scoped_store.part(intents[0]["part_id"]) if intents[0]["part_id"] else None
            baseline = float((part or {}).get("unit_cost") or 0)

        return scopes, {
            "path": "free_form",
            "actions": actions,
            "purchase_intents": intents,
            "part_id": intents[0]["part_id"] if intents else None,
            "needed_by": intents[0]["needed_by"] if intents else None,
            "worst_case_unit_price": max(
                (float(i["unit_price"] or 0) for i in intents), default=0.0
            ),
            "original_unit_price": baseline,
            "worst_case_value": round(value, 2),
        }

    @staticmethod
    def _check_plan_is_runnable(facts: dict) -> list[Check]:
        """Refuse a plan the gate could not even interpret.

        Fails closed and names why, rather than letting a malformed plan fall
        through the remaining rules, pass them vacuously because there is
        nothing to check, and reach a human as an approval request.
        """
        if facts.get("unknown_workflow"):
            return [Check(
                "workflow.exists", "fail",
                f"the plan names workflow {facts.get('workflow')!r}, which is "
                f"not registered",
                {"workflow": facts.get("workflow")},
            )]
        if facts.get("invalid_parameters"):
            missing = [
                ".".join(str(p) for p in e.get("loc", []))
                for e in facts["invalid_parameters"]
            ]
            return [Check(
                "workflow.parameters_valid", "fail",
                f"the plan chose {facts.get('workflow')} without valid "
                f"parameters: {', '.join(missing)}",
                {"workflow": facts.get("workflow"),
                 "supplied": facts.get("supplied"),
                 "errors": facts["invalid_parameters"]},
            )]
        return []

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

        # One normalised list for both paths. Deriving the part from the intent
        # rather than from a top-level field is what makes this rule apply to a
        # free-form purchase as well as to a workflow.
        intents = [i for i in facts.get("purchase_intents") or [] if i.get("supplier_id")]

        if rules.get("supplier_must_be_approved_for_part") and intents:
            offending = []
            for intent in intents:
                record = store.supplier(intent["supplier_id"]) or {}
                approved_parts = record.get("approved_parts") or []
                if not record.get("approved") or intent["part_id"] not in approved_parts:
                    offending.append(
                        {"supplier_id": intent["supplier_id"],
                         "part_id": intent["part_id"],
                         "name": record.get("name"),
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
                            f"{o['part_id']}" for o in offending
                        )
                    ) if offending else
                    "every supplier in play is approved for the part it would supply",
                    {"intents": intents, "offending": offending},
                )
            )
        part_id = facts.get("part_id")

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
