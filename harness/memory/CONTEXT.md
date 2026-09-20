# memory/: what outlives a run

One job: hold a small number of durable observations, each carrying its age.

## Inputs
- Working (this run): an observation the kernel chose to promote
- Reference (every run): the clock

## Process
1. `remember()` writes or updates a keyed fact and increments its count.
2. `recall()` returns facts under a prefix, aged, dropping anything stale.

## Outputs
- Rows in `memory`
- A `memory.promoted` audit entry naming what it replaced
- A `durable_memory` section in every context bundle

## Human check
Read what is in the table. If anything in it is a fact a system of record also
knows, delete it. There should be one answer to any question, and it should live
where the question is actually answered.

## The rule
**Remember what happened, never what is true.** Two kinds are promotable:
`supplier_slip` and `part_reroute`. Both are observations of events. The ERP
owns current state, and copying it here creates a second answer that starts
rotting the moment it is written.

## Why every fact carries its age
`recall()` returns `observed_at` and `age_days`, and drops anything past
`max_age_days`. A memory presented without an age is presented as a timeless
truth, which is how an agent ends up confidently acting on something that
stopped being true months ago.

## Why the gate never reads this
Memory reaches the planner and not the gate. The gate re-derives every fact it
decides on from the store. So a stale or poisoned memory can make a
recommendation worse, and cannot make an unauthorised action possible.
