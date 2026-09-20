"""Calendar context: the user's own schedule, plus availability for approvers.

Two different questions get two different answers, and the difference is the
point.

"What is on my calendar" returns events with titles and attendees, because they
are the user's own. "Is my approver around tomorrow" returns a boolean per day
and nothing else, because free and busy is what an organisation normally
exposes about a colleague and it is all the routing rule needs. The rule reads
"the approver's calendar shows them out the next day"; it does not need to know
that Dana is at a supplier site visit.

The availability window runs from today through the approval deadline horizon,
so the gate can answer the routing question without going back to a provider
mid-decision.
"""
from __future__ import annotations

from datetime import timedelta

from ..clock import Clock
from ..errors import ScopeDenied
from ..store import ScopedStore
from . import ProviderResult, provider

HORIZON_DAYS = 7


@provider(
    "calendar",
    description="The user's own events, plus free and busy for the approver chain.",
    scopes=["calendar:read"],
)
def gather(store: ScopedStore, clock: Clock, focus: dict) -> ProviderResult:
    result = ProviderResult(system="calendar")

    try:
        result.records["my_events"] = store.my_events()
    except ScopeDenied as error:
        result.note_denial(error)
        return result

    today = clock.today()
    result.records["today"] = today.isoformat()

    # Whoever might end up holding the approval. The gate decides which of
    # these it actually uses; the provider just makes sure it does not have to
    # ask again mid-decision.
    chain = [
        store.principal.user_id,
        store.principal.backup_approver_id,
        store.principal.manager_id,
        *(focus.get("additional_approvers") or []),
    ]
    availability: dict[str, dict[str, bool]] = {}
    for user_id in dict.fromkeys(u for u in chain if u):
        availability[user_id] = {
            (today + timedelta(days=offset)).isoformat(): store.is_out_of_office(
                user_id, today + timedelta(days=offset)
            )
            for offset in range(HORIZON_DAYS)
        }
    result.records["out_of_office_by_day"] = availability
    return result
