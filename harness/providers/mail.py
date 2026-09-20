"""Inbox context: recent mail, with the messages that mention the focus marked.

Relevance is marked, never enforced. The provider returns the whole recent
window and adds a `relevance` note to each message saying which focus token it
matched, if any. Two reasons.

The obvious one is that a keyword filter would have thrown away the supplier's
delay email if Rita had written "the PO" instead of "PO-77812", and a harness
that silently drops the one message the scenario turns on is worse than one
that shows the model six messages.

The less obvious one is that the noise is load bearing. `M-004` is an
unsolicited offer from a supplier who is cheaper, faster, and not approved for
this part. If the provider filtered it out, the run would never demonstrate
that the gate and the workflow are what stop it rather than luck. Showing the
model the bait and then refusing the bad action in code is the whole argument.
"""
from __future__ import annotations

from datetime import timedelta

from ..clock import Clock
from ..errors import ScopeDenied
from ..store import ScopedStore
from . import ProviderResult, provider

LOOKBACK_DAYS = 14


def _tokens(focus: dict, extra: list[str] | None = None) -> list[str]:
    values = [
        focus.get("part_id"),
        focus.get("po_id"),
        focus.get("prod_order_id"),
        focus.get("lot_id"),
        *(extra or []),
    ]
    return [str(v) for v in values if v]


@provider(
    "mail",
    description="The user's own inbox for the recent window, with focus matches marked.",
    scopes=["mail:read"],
)
def gather(store: ScopedStore, clock: Clock, focus: dict) -> ProviderResult:
    result = ProviderResult(system="mail")
    since = (clock.now() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    try:
        messages = store.inbox(since=since)
    except ScopeDenied as error:
        result.note_denial(error)
        return result

    tokens = _tokens(focus, focus.get("supplier_emails"))
    marked = []
    for message in messages:
        haystack = f"{message.get('subject', '')} {message.get('body', '')}"
        matched = [t for t in tokens if t in haystack or t in message.get("from", "")]
        marked.append(message | {"relevance": {"matched_focus_tokens": matched}})

    result.records["window_since"] = since
    result.records["messages"] = marked
    result.records["matched_message_ids"] = [
        m["message_id"] for m in marked if m["relevance"]["matched_focus_tokens"]
    ]
    return result
