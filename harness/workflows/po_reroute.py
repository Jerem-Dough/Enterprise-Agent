"""po_reroute v1: move an at-risk purchase order to an approved alternate.

Purchasing's words, implemented literally: confirm the alternate supplier is
approved for the part, confirm their lead time meets the production date,
create the new PO, cancel or reduce the old one, notify production, schedule
the arrival check. In that order, every time.

Seven steps, not six. Purchasing's list assumes a supplier already in hand, and
something has to pick it, so selection is step one inside the definition where
it is bounded and logged.

Three independent mechanisms keep that selection honest, and they fail
separately:

1. Code filters candidates to suppliers approved *for this part*.
2. That list becomes an enum in the request schema, so the model cannot name
   anything else. Added after a live call answered "S-Z Meridian Drives"
   instead of "S-Z".
3. The step re-checks the answer and step two re-derives approval from the
   supplier record.

The seeded world contains a supplier that is approved, cheapest on file, ships
next day, and emails an offer that morning. It is not approved for this part,
and it does not get past the first mechanism.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, create_model

from ..store import ScopedStore
from . import Step, StepContext, StepOutcome, WorkflowDefinition, register, tool_step


class PoRerouteParams(BaseModel):
    """What the planner supplies when it decides a reroute is the answer."""

    part_id: str = Field(description="The part at risk, e.g. P-4471.")
    original_po_id: str = Field(description="The purchase order that has slipped.")
    prod_order_id: str = Field(description="The production order at risk.")
    qty: int = Field(gt=0, description="Units the replacement order should cover.")
    needed_by: str = Field(
        description="ISO date the material must be on the dock, normally the "
                    "production order's scheduled start."
    )
    preferred_supplier_id: str | None = Field(
        default=None,
        description=(
            "Optional suggestion. The workflow chooses from its own filtered "
            "list of suppliers approved for this part and may disregard this."
        ),
    )
    justification: str = Field(
        min_length=20, description="Why a reroute is the right response."
    )


def approved_candidates(
    store: ScopedStore, part_id: str, exclude_supplier_id: str | None
) -> list[dict]:
    """Suppliers eligible for this part, computed in code.

    Both conditions matter and only one of them is obvious. `approved` says the
    supplier is on the approved vendor list at all. `part_id in approved_parts`
    says they are qualified for this specific part. A check that stops at the
    first is the bug this function exists to not have.
    """
    return sorted(
        (
            s for s in store.suppliers()
            if s.get("approved")
            and part_id in (s.get("approved_parts") or [])
            and s.get("supplier_id") != exclude_supplier_id
        ),
        key=lambda s: (int(s.get("lead_time_days", 99)),
                       float((s.get("pricing") or {}).get(part_id, 1e9))),
    )


# -- step 1: choose, from a list code produced ----------------------------


class SupplierChoice(BaseModel):
    """The unconstrained shape, kept for reference and for the cassette schema
    name. The step actually calls the model with `choice_model()` below."""

    supplier_id: str = Field(description="Must be one of the offered candidates.")
    justification: str = Field(
        min_length=20,
        description="Why this candidate over the others, in one or two sentences.",
    )


def choice_model(candidate_ids: list[str]) -> type[BaseModel]:
    """Build the output schema from the candidate list, at call time.

    The difference between instructing a model and constraining one. Asked in
    prose for an id from a list, a live call returned "S-Z Meridian Drives":
    reasonable, and outside the set. As an enum the model cannot emit anything
    else. `_choose_supplier` checks anyway, because the workflow should not be
    where we discover the guarantee did not hold.
    """
    return create_model(
        "SupplierChoice",
        supplier_id=(
            Literal[tuple(candidate_ids)],  # type: ignore[valid-type]
            Field(description="Exactly one of the offered candidate ids."),
        ),
        justification=(
            str,
            Field(min_length=20,
                  description="Why this candidate over the others, in one or two "
                              "sentences. Name the consideration that decided it."),
        ),
    )


CHOICE_SYSTEM = """\
You are choosing a replacement supplier inside a fixed purchasing workflow.

You will be given a list of candidates. That list has already been filtered to
suppliers who are approved for this specific part. You may choose only from it.

Return the supplier id exactly as it appears in the list, for example S-Z. Do
not include the supplier name in that field; it belongs in your justification.

Choose on whether the lead time meets the date the material is needed, first,
and on unit price second. A cheaper supplier that arrives too late is the wrong
answer. Say which consideration decided it.
"""


def _choose_supplier(context: StepContext) -> StepOutcome:
    params: PoRerouteParams = context.params
    original = context.store.purchase_order(params.original_po_id)
    exclude = (original or {}).get("supplier_id")
    candidates = approved_candidates(context.store, params.part_id, exclude)

    if not candidates:
        return StepOutcome(
            ok=False,
            error={
                "error": "NoApprovedAlternate",
                "message": (
                    f"no supplier other than {exclude} is approved for "
                    f"{params.part_id}"
                ),
            },
        )

    offered = [
        {
            "supplier_id": s["supplier_id"],
            "name": s["name"],
            "lead_time_days": s["lead_time_days"],
            "unit_price": (s.get("pricing") or {}).get(params.part_id),
        }
        for s in candidates
    ]

    # Nothing to choose. Skip the call rather than spend a request confirming
    # the only option.
    if len(offered) == 1:
        return StepOutcome(
            ok=True,
            output={
                "supplier_id": offered[0]["supplier_id"],
                "justification": "the only supplier approved for this part",
                "candidates_offered": offered,
                "chosen_by": "code",
            },
        )

    today = context.clock.today()
    user = (
        f"Part: {params.part_id}\n"
        f"Quantity: {params.qty}\n"
        f"Needed on the dock by: {params.needed_by}\n"
        f"Today: {today.isoformat()}\n"
        f"Planner's suggestion (not binding): {params.preferred_supplier_id}\n\n"
        f"Candidates:\n"
        + "\n".join(
            f"  - {c['supplier_id']} {c['name']}: lead time {c['lead_time_days']} "
            f"days, unit price {c['unit_price']}"
            for c in offered
        )
    )
    choice, call = context.llm.structured(
        purpose="workflow.select_supplier",
        situation=f"{context.situation}:{context.step_id}",
        system=CHOICE_SYSTEM,
        user=user,
        # The schema itself carries the candidate list as an enum.
        output_model=choice_model([c["supplier_id"] for c in offered]),
        max_tokens=2000,
    )

    # The boundary. An answer outside the offered set fails the step.
    allowed = {c["supplier_id"] for c in offered}
    if choice.supplier_id not in allowed:
        return StepOutcome(
            ok=False,
            error={
                "error": "ChoiceOutOfBounds",
                "message": (
                    f"the model named {choice.supplier_id}, which was not among "
                    f"the candidates offered: {sorted(allowed)}"
                ),
            },
        )

    return StepOutcome(
        ok=True,
        output={
            "supplier_id": choice.supplier_id,
            "justification": choice.justification,
            "candidates_offered": offered,
            "chosen_by": "model",
            "model_call": call.key,
            "replayed": call.replayed,
        },
    )


# -- steps 2 and 3: confirm, in code --------------------------------------


def _confirm_approved(context: StepContext) -> StepOutcome:
    params: PoRerouteParams = context.params
    supplier_id = context.output_of("select_alternate_supplier")["supplier_id"]
    supplier = context.store.supplier(supplier_id)

    if supplier is None:
        return StepOutcome(ok=False, error={"error": "UnknownSupplier",
                                            "message": f"no supplier {supplier_id}"})
    approved_parts = supplier.get("approved_parts") or []
    if not supplier.get("approved") or params.part_id not in approved_parts:
        return StepOutcome(
            ok=False,
            error={
                "error": "SupplierNotApprovedForPart",
                "message": (
                    f"{supplier_id} ({supplier.get('name')}) is not approved for "
                    f"{params.part_id}"
                ),
                "detail": {"approved": supplier.get("approved"),
                           "approved_parts": approved_parts},
            },
        )
    return StepOutcome(
        ok=True,
        output={"supplier_id": supplier_id, "supplier_name": supplier["name"],
                "approved_for_part": True, "approved_parts": approved_parts},
    )


def _confirm_lead_time(context: StepContext) -> StepOutcome:
    params: PoRerouteParams = context.params
    supplier_id = context.output_of("select_alternate_supplier")["supplier_id"]
    supplier = context.store.supplier(supplier_id)
    lead_time = int(supplier.get("lead_time_days", 99))
    arrival = context.clock.today() + timedelta(days=lead_time)
    needed_by = date.fromisoformat(params.needed_by)

    if arrival > needed_by:
        return StepOutcome(
            ok=False,
            error={
                "error": "LeadTimeMissesProductionDate",
                "message": (
                    f"{supplier_id} quotes {lead_time} days, arriving "
                    f"{arrival.isoformat()}, after the {params.needed_by} required"
                ),
                "detail": {"lead_time_days": lead_time,
                           "projected_arrival": arrival.isoformat(),
                           "needed_by": params.needed_by},
            },
        )
    return StepOutcome(
        ok=True,
        output={"lead_time_days": lead_time,
                "projected_arrival": arrival.isoformat(),
                "needed_by": params.needed_by,
                "days_of_margin": (needed_by - arrival).days},
    )


# -- steps 4 and 5: write -------------------------------------------------


def _create_replacement(context: StepContext) -> StepOutcome:
    params: PoRerouteParams = context.params
    supplier_id = context.output_of("select_alternate_supplier")["supplier_id"]
    supplier = context.store.supplier(supplier_id)
    unit_price = float((supplier.get("pricing") or {}).get(params.part_id, 0))

    return tool_step(context, "create_purchase_order", {
        "part_id": params.part_id,
        "supplier_id": supplier_id,
        "qty": params.qty,
        "unit_price": unit_price,
        "needed_by": params.needed_by,
        "reason": (
            f"Replacement for {params.original_po_id}, which will not arrive "
            f"before production order {params.prod_order_id} starts on "
            f"{params.needed_by}. {params.justification}"
        ),
    })


def _amend_original(context: StepContext) -> StepOutcome:
    """Cancel outright, or cut the quantity. Decided in code, not by the model.

    The rule is arithmetic: if the replacement covers the full original
    quantity, the original is cancelled. If it covers only part, the original
    is reduced by that part, because the remainder is still wanted and
    cancelling it would create a second shortage.
    """
    params: PoRerouteParams = context.params
    original = context.store.purchase_order(params.original_po_id)
    if original is None:
        return StepOutcome(ok=False, error={"error": "UnknownPurchaseOrder",
                                            "message": params.original_po_id})

    original_qty = int(original.get("qty", 0))
    replacement_po = context.output_of("create_replacement_po").get("po_id")

    if params.qty >= original_qty:
        action, new_qty = "cancel", None
        note = f"fully replaced by {replacement_po}"
    else:
        action, new_qty = "reduce", original_qty - params.qty
        note = f"reduced by {params.qty} now covered by {replacement_po}"

    call = {
        "po_id": params.original_po_id,
        "action": action,
        "reason": f"Supply rerouted for production order {params.prod_order_id}: {note}",
    }
    if new_qty is not None:
        call["new_qty"] = new_qty
    return tool_step(context, "amend_purchase_order", call)


# -- step 6: notify, with the text bounded --------------------------------


class NotificationDraft(BaseModel):
    subject: str = Field(max_length=110)
    body: str = Field(min_length=60, max_length=1000)


DRAFT_SYSTEM = """\
Write a short internal notice to a production supervisor about a change in
material supply for one of their orders.

Constraints, all of them hard:
- State only the facts given to you. Do not add a cause, an apology, a promise,
  or a detail that is not in the input.
- Name the production order, the new supplier, and the date the material is
  expected.
- Plain text. No greeting, no sign-off, no bullet characters.
- No em dashes and no double hyphens. Use a full stop when the two halves are
  whole thoughts, a comma for an aside, a colon when the second half defines
  the first.
- Four sentences at most.
"""

BANNED_PUNCTUATION = ("\u2014", "\u2013", " -- ")


def _fallback_notice(order_id, supplier_name, arrival, part_id) -> tuple[str, str]:
    return (
        f"Supply change for production order {order_id}",
        f"Material supply for production order {order_id} has changed. "
        f"{part_id} is now coming from {supplier_name} and is expected on "
        f"{arrival}. The previous order has been amended. No action is needed "
        f"from you unless the material has not arrived by that date.",
    )


def _notify(context: StepContext) -> StepOutcome:
    params: PoRerouteParams = context.params
    chosen = context.output_of("confirm_supplier_approved")
    lead = context.output_of("confirm_lead_time")
    supplier_name = chosen["supplier_name"]
    arrival = lead["projected_arrival"]

    subject, body = _fallback_notice(
        params.prod_order_id, supplier_name, arrival, params.part_id
    )
    used_fallback, reason = True, "not attempted"

    try:
        draft, call = context.llm.structured(
            purpose="workflow.draft_notification",
            situation=f"{context.situation}:{context.step_id}",
            system=DRAFT_SYSTEM,
            user=(
                f"Production order: {params.prod_order_id}\n"
                f"Part: {params.part_id}\n"
                f"New supplier: {supplier_name}\n"
                f"Expected on the dock: {arrival}\n"
                f"Previous order {params.original_po_id} has been amended."
            ),
            output_model=NotificationDraft,
            max_tokens=1500,
        )
        # The draft has to carry the facts it was asked to carry. This is the
        # bound on a generative step: not "did it sound right" but "does it
        # contain the three things a supervisor needs".
        missing = [
            label for label, token in (
                ("production order", params.prod_order_id),
                ("supplier", supplier_name),
                ("arrival date", arrival),
            )
            if token not in draft.body
        ]
        # House style is a hard rule on anything a colleague reads, and a
        # prompt cannot guarantee it: one live run came back with an em dash
        # in otherwise perfect text. Rewriting the punctuation here is banned,
        # because a blind substitution produces sentences like "deliberately
        # quiet. a ledger, a queue". So the draft is rejected and the
        # deterministic template goes out instead, which is known good prose.
        offending = [
            p for p in BANNED_PUNCTUATION if p in draft.body or p in draft.subject
        ]
        if missing:
            reason = f"draft omitted: {', '.join(missing)}"
        elif offending:
            reason = "draft used punctuation the house style forbids"
        else:
            subject, body = draft.subject, draft.body
            used_fallback, reason = False, ""
    except Exception as error:  # noqa: BLE001
        reason = f"{type(error).__name__}: {error}"

    outcome = tool_step(context, "notify_production", {
        "prod_order_id": params.prod_order_id,
        "subject": subject,
        "body": body,
    })
    if outcome.ok:
        outcome.output |= {"used_fallback_text": used_fallback,
                           "fallback_reason": reason}
    return outcome


# -- step 7: come back and check ------------------------------------------


def _schedule_check(context: StepContext) -> StepOutcome:
    params: PoRerouteParams = context.params
    lead = context.output_of("confirm_lead_time")
    new_po = context.output_of("create_replacement_po").get("po_id")

    return tool_step(context, "schedule_follow_up", {
        "check": "po_arrival",
        "due_date": lead["projected_arrival"],
        "subject_user": context.store.principal.user_id,
        "context": {
            "po_id": new_po,
            "part_id": params.part_id,
            "prod_order_id": params.prod_order_id,
            "needed_by": params.needed_by,
        },
        "reason": (
            f"Confirm the replacement shipment for production order "
            f"{params.prod_order_id} actually arrived."
        ),
    })


# -- gate facts -----------------------------------------------------------


def _gate_facts(params: PoRerouteParams, store: ScopedStore) -> dict:
    """Policy-relevant facts derivable before any step runs.

    The value is the worst case across the candidates, not the cheapest,
    because approval is granted before the supplier is chosen. Gating on the
    best case would let a plan clear a threshold and then execute above it.
    """
    original = store.purchase_order(params.original_po_id)
    exclude = (original or {}).get("supplier_id")
    candidates = approved_candidates(store, params.part_id, exclude)
    prices = [
        float((s.get("pricing") or {}).get(params.part_id, 0)) for s in candidates
    ]
    worst_price = max(prices) if prices else 0.0

    return {
        "action": "create_purchase_order",
        "part_id": params.part_id,
        "supplier_candidates": [s["supplier_id"] for s in candidates],
        "preferred_supplier_id": params.preferred_supplier_id,
        "qty": params.qty,
        "worst_case_unit_price": worst_price,
        "worst_case_value": round(params.qty * worst_price, 2),
        "needed_by": params.needed_by,
        "original_po_id": params.original_po_id,
        "original_unit_price": float((original or {}).get("unit_price", 0)),
        "original_value": float((original or {}).get("total_value", 0)),
        "prod_order_id": params.prod_order_id,
    }


DEFINITION = register(
    WorkflowDefinition(
        name="po_reroute",
        version="1.0.0",
        description=(
            "Move an at-risk purchase order to an approved alternate supplier, "
            "amend the original, tell production, and schedule an arrival check. "
            "Enter this when an open order will not arrive before a production "
            "order that depends on it starts."
        ),
        params_model=PoRerouteParams,
        gate_facts=_gate_facts,
        steps=(
            Step("select_alternate_supplier",
                 "Choose among suppliers already filtered to those approved for "
                 "this part. Bounded model step.",
                 "model", _choose_supplier),
            Step("confirm_supplier_approved",
                 "Re-derive from the supplier record that the chosen supplier is "
                 "approved for this part.",
                 "check", _confirm_approved),
            Step("confirm_lead_time",
                 "Confirm the supplier's lead time puts the material on the dock "
                 "before the production order starts.",
                 "check", _confirm_lead_time),
            Step("create_replacement_po",
                 "Place the replacement purchase order.",
                 "tool", _create_replacement, tool="create_purchase_order"),
            Step("amend_original_po",
                 "Cancel the original order, or reduce it by the quantity now "
                 "covered.",
                 "tool", _amend_original, tool="amend_purchase_order"),
            Step("notify_production",
                 "Tell the production supervisor. Body drafted by a bounded "
                 "model step with a deterministic fallback.",
                 "tool", _notify, tool="notify_production"),
            Step("schedule_arrival_check",
                 "Queue a check for the promised arrival date.",
                 "tool", _schedule_check, tool="schedule_follow_up"),
        ),
    )
)
