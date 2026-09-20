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


class Plan(BaseModel):
    """What the planner returns."""

    headline: str = Field(
        min_length=20,
        max_length=400,
        description=(
            "What to tell the person, in the voice of their own agent. State the "
            "risk, the cause, and the proposed action. This is the text they see."
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

    def is_write(self) -> bool:
        return bool(self.workflow or self.actions)

    def as_dict(self) -> dict:
        return self.model_dump(mode="json")
