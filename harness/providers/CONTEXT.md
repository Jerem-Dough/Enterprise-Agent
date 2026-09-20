# providers/: context per system, scoped to the user

One job: turn a focus into the records from one system that bear on it.
Providers decide nothing.

## Inputs
- Working (this run): the attention item's `focus` dict
- Reference (every run): a `ScopedStore` for the principal, and the clock

## Process
1. The kernel skips any provider whose scopes the principal wholly lacks.
2. Each remaining provider reads what it can through the scoped handle.
3. A `ScopeDenied` is caught and recorded in `omitted`, not raised.

## Outputs
- A `ProviderResult` per system, carrying `records` and `omitted`
- Assembled into the bundle at `runs/<run-id>/02-context.json`

## Human check
Open a run's `02-context.json`. Every record should be addressable by system,
kind and id. If prose has appeared in it, a provider has started summarising.
That is the planner's job, and it removes the reader's ability to check a claim.

## Adding one
Drop a module here, decorate with `@provider(name, description=, scopes=)`, and
add it to `_load()`. Nothing else changes.

## Why relevance is marked and not filtered
The mail provider returns the whole recent window and tags which messages match
the focus. Filtering would have dropped the supplier's delay email if she had
written "the PO" instead of "PO-77812", and it would have hidden the bait offer
from the unapproved supplier, which is the thing the gate exists to survive.
Showing the model the bait and refusing the bad action in code is the argument.
