# tools/: the things the agent can actually do

One job: typed, scoped, idempotent, reversible actions. Every write in the
system passes through `ToolRunner.invoke`.

## Inputs
- Working (this run): parameters from a plan or a workflow step
- Reference (every run): a `ScopedStore`, the clock, the scheduler

## Process
1. Idempotency lookup. An invocation already recorded returns its first result.
2. Scope check at the door, ahead of the store's own check.
3. Pydantic validation of the parameters.
4. The write, the invocation record and the audit entry, in one transaction.

## Outputs
- A `ToolResult`
- A row in `tool_invocations`
- One `tool.invoked`, `tool.denied`, `tool.invalid_params` or `tool.failed`
  audit entry

## Human check
For any tool you add, ask what happens if it runs twice. If the answer is not
"nothing", the idempotency key is wrong.

## Adding one
Declare a Pydantic params model, write the runner, write the compensation,
decorate with `@tool(...)`, and add the module to `_load()`. The same
declaration produces the validation and the JSON schema shown to the model.

## Write the compensation honestly
`reallocate_lot` reverses cleanly and reports `effect_reversed: True`. A
notification cannot be unsent, so `notify_production` sends a correction and
reports `effect_reversed: False`. An engine that believed the step had been
erased would be lying in a log whose whole value is that it does not.

Compensation restores the system of record. It does not restore the world.
