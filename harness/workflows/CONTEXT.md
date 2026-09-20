# workflows/: declared graphs, where the definition is in charge

One job: run a fixed sequence of steps, in a fixed order, every time.

## Inputs
- Working (this run): the parameters the planner supplied
- Reference (every run): the definition, a `ScopedStore`, the tool runner, and
  the model client

## Process
1. `start()` validates the parameters and writes an instance.
2. `run()` walks `definition.steps` by index from the persisted cursor.
3. Each step's row, its output and the new cursor commit in one transaction.
4. A failed step compensates completed steps in reverse, as the same principal.

## Outputs
- Rows in `workflow_instances` and `workflow_steps`
- One file per step under `runs/<run-id>/07-steps/`
- Audit entries for started, each step, and completed or failed

## Human check
Kill a run mid-workflow with `--stop-before <step>`, then resume it. The world
must end up as if it had never been interrupted, with no duplicate write.

## What makes the guarantee real rather than intended
- `steps` is an immutable tuple walked by index. No parameter, plan field or
  model output can reorder, skip or insert a step.
- `Plan` refuses to carry both a workflow and free-form actions, so a model
  cannot append a step by putting it somewhere else.
- Idempotency keys are `instance:step`, not a hash of the arguments, so a
  parameter recomputed slightly differently on resume cannot cause a second
  write.

## Bounding a model step
Code computes the candidate set. The model picks from it. The answer is
validated against the set. See `select_alternate_supplier` in `po_reroute.py`:
if the model can name something the code did not offer, the step is not bounded,
it is merely supervised.

A generative step needs a deterministic fallback. `notify_production` checks the
draft for the facts it must carry and falls back to a template when they are
missing. A language failure must not become an outage.
