# detect/: noticing, on a schedule, without being asked

One job: decide whether a situation exists that a particular employee would want
to know about. Detectors do not gather full context, reason, or propose.

## Inputs
- Reference (every run): a `ScopedStore` per subscribed user, and the clock

## Process
1. The sweep enumerates users with the privileged handle.
2. Each detector runs against the scoped handle of a user whose role subscribes
   to it and who holds every scope it reads.
3. Each yielded item is persisted unless its dedupe key is already open.

## Outputs
- Rows in `attention_items`
- A `SweepResult` of new, suppressed and skipped

## Human check
Run `python harmony.py detect` twice. The second run must produce nothing new. Then
change a material fact in `company/` and run it again: it must produce one item.

## Adding one
Drop a module here, decorate with `@detector(name, description=, roles=,
scopes=)`, and add it to `_load()`.

## The two rules that matter
**Detectors are deterministic.** No model runs here. A detector that fires on a
model's judgement has a false positive rate nobody can reason about. Where
language has to be understood, that happens later, in the planner, over a
context bundle a human can read.

**Dedupe keys describe the situation, not the alert.** Build the key from the
facts that would make this a genuinely new problem. A repeated sweep produces
one item. A second message from the supplier produces a second.
