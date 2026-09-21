"""The shape a plan has to arrive in.

Kept in its own module because three different things depend on it and none of
them should depend on the model client: the planner produces it, the gate
consumes it, and the executor walks it. Keeping the contract separate from the
thing that calls an API is what lets every test below the planner run without a
network.

The important constraint is in `Plan`: a plan either enters a declared workflow
or proposes free-form actions, never both. That is not a style preference. If a
plan could do both, the model would be able to append a step to a workflow by
putting it in `actions`, and the guarantee that purchasing asked for ("in that
order, every time") would hold only by convention. Making it a validated
either-or means the guarantee is enforced by the parser, before the gate ever
sees the plan.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ProposedAction(BaseModel):
    """One free-form action. Scenario B's path."""

    tool: str = Field(description="Tool name, exactly as it appears in the catalogue.")
    params: dict = Field(
        default_factory=dict, description="Arguments matching that tool's schema."
    )
    rationale: str = Field(
        min_length=10,
        description="Why this action, in one sentence. Recorded in the audit log.",
    )


class Citation(BaseModel):
    """A claim tied to the record that supports it.

    Every factual assertion in the recommendation has to point at something in
    the context bundle. A recommendation the reader cannot check is a guess with
    good grammar, and this is the field that makes checking mechanical.
    """

    claim: str = Field(min_length=5, description="The assertion being made.")
    system: str = Field(description="erp, mail, calendar or quality.")
    record_id: str = Field(description="The id of the record that supports it.")


class PlanDraft(BaseModel):
    """What the model is actually asked for: a path, not its parameters.

    `Plan.workflow_params` is an open dict, and an open dict is exactly what
    constrained decoding cannot fill. Pydantic renders `dict` as
    `{"type": "object"}` with no properties, so a model decoding against it can
    emit `{}` and nothing else. A live run proved it: the model chose
    `po_reroute` correctly and returned empty parameters twice, once on the
    retry that handed it the validation error verbatim. It was not ignoring the
    instruction. It was obeying a schema that had no room for the answer.

    So this step decides *which* path, and a second call fills the parameters
    against the chosen workflow's own model, where every field is concrete and
    the schema can express them. The planner decides whether; a bounded call
    supplies what. That is the same shape the workflow's own model steps use.
    """

    headline: str = Field(
        min_length=20,
        max_length=700,
        description=(
            "What to tell the person, in the voice of their own agent. State the "
            "risk, the cause, and the proposed action, then ask. This is the text "
            "they see. Aim for about 60 words and stay under 700 characters."
        ),
    )
    reasoning: str = Field(
        min_length=40,
        description="How the conclusion follows from the context. For the audit log.",
    )
    citations: list[Citation] = Field(
        default_factory=list, description="Every factual claim, tied to a record."
    )
    workflow: str | None = Field(
        default=None,
        description=(
            "Name of a declared workflow to enter, if one covers this situation. "
            "Its parameters are asked for separately, so name it here and do not "
            "restate its steps as actions."
        ),
    )
    actions: list[ProposedAction] = Field(
        default_factory=list,
        description=(
            "Free-form actions, used only when no workflow covers the situation. "
            "Must be empty when workflow is set."
        ),
    )
    no_action_reason: str | None = Field(
        default=None,
        description=(
            "Set this instead of a workflow or actions when the right answer is "
            "to do nothing. Deciding not to act is a valid outcome."
        ),
    )
    confidence: Literal["high", "medium", "low"] = "medium"

    @model_validator(mode="after")
    def _exactly_one_path(self) -> PlanDraft:
        chosen = [bool(self.workflow), bool(self.actions), bool(self.no_action_reason)]
        if sum(chosen) != 1:
            raise ValueError(
                "a plan must do exactly one of: enter a workflow, propose "
                "free-form actions, or give a reason for taking no action"
            )
        return self


class Plan(BaseModel):
    """What the planner returns."""

    headline: str = Field(
        min_length=20,
        max_length=700,
        description=(
            "What to tell the person, in the voice of their own agent. State the "
            "risk, the cause, and the proposed action, then ask. This is the text "
            "they see. Aim for about 60 words and stay under 700 characters."
        ),
    )
    """700, not 400.

    A live run produced a 410 character headline that was correct in every
    respect, and the whole plan was thrown away validating it. The number was a
    guess about how long "one short paragraph" is, and it was wrong: a real
    headline naming two dates, a part, a purchase order and a supplier does not
    fit in 400. A constraint that discards good work is not a safety property,
    it is a bug with a schema in front of it.

    The length still has a ceiling, because the field is rendered to a person
    and something has to stop a model writing an essay into it. The guidance
    now lives in the description too, where the model actually reads it.
    """
    reasoning: str = Field(
        min_length=40,
        description="How the conclusion follows from the context. For the audit log.",
    )
    citations: list[Citation] = Field(
        default_factory=list, description="Every factual claim, tied to a record."
    )

    workflow: str | None = Field(
        default=None,
        description=(
            "Name of a declared workflow to enter, if one covers this situation. "
            "When set, the workflow's definition decides the steps and their order."
        ),
    )
    workflow_params: dict = Field(
        default_factory=dict, description="Inputs for that workflow."
    )
    actions: list[ProposedAction] = Field(
        default_factory=list,
        description=(
            "Free-form actions, used only when no workflow covers the situation. "
            "Must be empty when workflow is set."
        ),
    )

    no_action_reason: str | None = Field(
        default=None,
        description=(
            "Set this instead of a workflow or actions when the right answer is "
            "to do nothing. Deciding not to act is a valid outcome."
        ),
    )
    confidence: Literal["high", "medium", "low"] = "medium"

    @model_validator(mode="after")
    def _exactly_one_path(self) -> Plan:
        chosen = [
            bool(self.workflow),
            bool(self.actions),
            bool(self.no_action_reason),
        ]
        if sum(chosen) != 1:
            raise ValueError(
                "a plan must do exactly one of: enter a workflow, propose "
                "free-form actions, or give a reason for taking no action"
            )
        return self

    @model_validator(mode="after")
    def _a_workflow_needs_its_parameters(self) -> Plan:
        """Entering a workflow without filling in its parameters is not a plan.

        `workflow_params` is an open dict, because this schema is static and
        cannot know which of several workflows the model will pick. A live run
        exploited exactly that gap: it set `workflow` correctly and left
        `workflow_params` as `{}`, having been told the parameter list in the
        system prompt rather than in the schema it was decoding against.

        Catching it here turns a downstream crash into a validation error the
        model is handed back and given one chance to fix, which is cheaper than
        either a second call or a discriminated union over every registered
        workflow. Per-parameter validation still happens at the gate, against
        the workflow's own model.
        """
        if self.workflow and not self.workflow_params:
            raise ValueError(
                f"workflow {self.workflow!r} was chosen but workflow_params is "
                f"empty; fill in every parameter that workflow declares"
            )
        return self

    def is_write(self) -> bool:
        return bool(self.workflow or self.actions)

    def as_dict(self) -> dict:
        return self.model_dump(mode="json")
