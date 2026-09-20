"""A scripted model client, for tests and for running without a key.

This is not a mock of the Claude API. It implements the same narrow interface
the harness actually depends on: given a purpose and a situation, return a
validated instance of the requested schema. Everything below the planner is
deterministic anyway, so scripting this one seam makes the gate, the workflow
engine, the scheduler and the audit chain testable without a network.

The scripts are deliberately *plausible rather than ideal*, including one that
is wrong on purpose. `bad_supplier_choice` names a supplier that was never
offered, which is how the test suite proves that the bound on the model step is
enforced by code and not by the model's good behaviour.
"""
from __future__ import annotations

from collections.abc import Callable

from harness.plan.llm import LLMClient, ModelCall, ModelUnavailable, cassette_key


class ScriptedClient(LLMClient):
    """Answers by purpose. Raises loudly on anything it was not given."""

    def __init__(self, script: dict[str, dict | Callable[[str], dict]]) -> None:
        self.script = script
        self.calls: list[ModelCall] = []

    def structured(self, *, purpose, situation, system, user, output_model,
                   max_tokens=8000):
        if purpose not in self.script:
            raise ModelUnavailable(
                f"the script has no answer for purpose {purpose!r}; "
                f"situations seen so far: {[c.purpose for c in self.calls]}"
            )
        entry = self.script[purpose]
        payload = entry(user) if callable(entry) else entry
        parsed = output_model.model_validate(payload)
        call = ModelCall(
            key=cassette_key(purpose, situation, output_model.__name__),
            purpose=purpose, model="scripted", system=system, user=user,
            schema_name=output_model.__name__,
            output=parsed.model_dump(mode="json"),
            usage={"input_tokens": len(user) // 4, "output_tokens": 0},
        )
        self.calls.append(call)
        return parsed, call


# -- Scenario A -----------------------------------------------------------

REROUTE_PLAN = {
    "headline": (
        "P-4471 will likely cause production order 4812 to miss its scheduled "
        "start on 2026-09-07. Kestrel Components says PO-77812 is now landing "
        "Tuesday 9/8. I can move the order to an approved alternate supplier "
        "and notify production. Want me to proceed?"
    ),
    "reasoning": (
        "On hand is 150 against 30 a day, so stock runs out on 2026-09-07, the "
        "day production order 4812 starts. That order needs 120 units. The only "
        "open order for the part is PO-77812 with Kestrel, promised 2026-09-04, "
        "and Rita Alvarez wrote on 2026-09-01 that the revised ship date puts "
        "it on the dock Tuesday 9/8, which is after the start. Rerouting to an "
        "approved alternate is the declared response for this situation."
    ),
    "citations": [
        {"claim": "150 on hand against 30 a day of usage",
         "system": "erp", "record_id": "P-4471"},
        {"claim": "PO-77812 is open and promised 2026-09-04",
         "system": "erp", "record_id": "PO-77812"},
        {"claim": "production order 4812 starts 2026-09-07 and needs 120",
         "system": "erp", "record_id": "4812"},
        {"claim": "the supplier moved the dock date to Tuesday 9/8",
         "system": "mail", "record_id": "M-001"},
    ],
    "workflow": "po_reroute",
    "workflow_params": {
        "part_id": "P-4471",
        "original_po_id": "PO-77812",
        "prod_order_id": "4812",
        "qty": 400,
        "needed_by": "2026-09-07",
        "preferred_supplier_id": None,
        "justification": (
            "The open order will not arrive before production order 4812 starts."
        ),
    },
    "actions": [],
    "no_action_reason": None,
    "confidence": "high",
}

SUPPLIER_CHOICE = {
    "supplier_id": "S-Z",
    "justification": (
        "Meridian Drives quotes two days, which puts the material on the dock "
        "well before the 2026-09-07 start. Foundry Line Supply is cheaper per "
        "unit but quotes nine days and misses the date entirely."
    ),
}

BAD_SUPPLIER_CHOICE = {
    "supplier_id": "S-Q",
    "justification": (
        "Apex Rapid Components quote 38.90 with next day shipping, which is "
        "both cheaper and faster than every other option on the table."
    ),
}

NOTIFICATION_DRAFT = {
    "subject": "Supply change for production order 4812",
    "body": (
        "Material supply for production order 4812 has changed. The stepper "
        "motors are now coming from Meridian Drives and are expected on the "
        "dock on 2026-09-04. The previous order with Kestrel Components has "
        "been amended. Flag it to purchasing if the material has not arrived "
        "by that date."
    ),
}

BAD_NOTIFICATION_DRAFT = {
    "subject": "Supply update",
    "body": (
        "Your parts situation has been resolved and everything is now on track. "
        "There is nothing further you need to do about this at the present time, "
        "and the line should run exactly as planned this week."
    ),
}

# -- Scenario B -----------------------------------------------------------

REALLOCATE_PLAN = {
    "headline": (
        "Lot L-2093 of P-1180 is on hold for surface finish and production "
        "order 4820 is still allocated to it, starting 2026-09-05 and needing "
        "80. Lot L-2101 is released, unallocated and holds 90. I can reallocate "
        "4820 to L-2101 and tell the supervisor. Want me to proceed?"
    ),
    "reasoning": (
        "L-2093 was placed on hold on 2026-09-02 for a 3.4 Ra surface finish "
        "against a 3.2 Ra spec, and production order 4820 starts on 2026-09-05 "
        "needing 80 units of P-1180. L-2101 was received on 2026-09-01, is "
        "released, is allocated to nothing, and holds 90, so it covers the "
        "requirement. No purchasing action is needed because internal coverage "
        "exists. No declared workflow covers lot reallocation, so this runs as "
        "individual actions."
    ),
    "citations": [
        {"claim": "L-2093 is on hold for surface finish",
         "system": "quality", "record_id": "L-2093"},
        {"claim": "production order 4820 starts 2026-09-05 and needs 80",
         "system": "erp", "record_id": "4820"},
        {"claim": "L-2101 is released, unallocated and holds 90",
         "system": "quality", "record_id": "L-2101"},
    ],
    "workflow": None,
    "workflow_params": {},
    "actions": [
        {
            "tool": "reallocate_lot",
            "params": {
                "prod_order_id": "4820",
                "part_id": "P-1180",
                "from_lot": "L-2093",
                "to_lot": "L-2101",
                "reason": "L-2093 is on hold for surface finish, L-2101 covers the 80 required",
            },
            "rationale": "L-2101 is the only released, unallocated lot that covers 80 units.",
        },
        {
            "tool": "notify_production",
            "params": {
                "prod_order_id": "4820",
                "subject": "Lot reallocation for production order 4820",
                "body": (
                    "Production order 4820 has been reallocated from lot L-2093 "
                    "to lot L-2101 for P-1180. L-2093 is on quality hold for "
                    "surface finish. L-2101 is released and covers the 80 units "
                    "required. No change to your scheduled start."
                ),
            },
            "rationale": "The supervisor needs to know which lot to pull.",
        },
    ],
    "no_action_reason": None,
    "confidence": "high",
}


def scenario_a(*, bad_choice: bool = False, bad_draft: bool = False) -> ScriptedClient:
    return ScriptedClient({
        "plan": REROUTE_PLAN,
        "workflow.select_supplier":
            BAD_SUPPLIER_CHOICE if bad_choice else SUPPLIER_CHOICE,
        "workflow.draft_notification":
            BAD_NOTIFICATION_DRAFT if bad_draft else NOTIFICATION_DRAFT,
    })


def scenario_b() -> ScriptedClient:
    return ScriptedClient({"plan": REALLOCATE_PLAN})


def combined(**kwargs) -> ScriptedClient:
    """One client that answers both scenarios, keyed by what the plan is for."""
    a = scenario_a(**kwargs)

    def route_plan(user: str) -> dict:
        return REALLOCATE_PLAN if "lot_hold" in user or "L-2093" in user else REROUTE_PLAN

    return ScriptedClient(a.script | {"plan": route_plan})
