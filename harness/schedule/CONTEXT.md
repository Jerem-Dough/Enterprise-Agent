# schedule/: deferred work that survives a restart

One job: hold work until it is due, then hand it to a caller.

## Inputs
- Working (this run): a kind, a due time, a payload, a dedupe key
- Reference (every run): the clock

## Process
1. `schedule()` writes a row. A duplicate dedupe key returns the first task.
2. `due()` asks the virtual clock what has come due.
3. `run_due()` marks a task fired, then calls the handler.

## Outputs
- Rows in `scheduled_tasks`
- `task.scheduled`, `task.fired` and `task.cancelled` audit entries

## Human check
Kill the process with work queued, restart, advance the clock, and tick. The
work must fire. Nothing is held in memory, so this should be uninteresting.

## Firing is not authorisation
A task carries a `subject_user`, and never scopes and never a token. When it
fires, the kernel resolves that user afresh and the work is gated again, as
them, at that moment. A follow up scheduled on Wednesday for somebody who lost
an entitlement on Thursday must not run with Wednesday's permissions.

## Why a task is marked fired before the handler runs
A handler that crashes leaves a task that does not fire again on the next tick.
For work that writes to an ERP, that is the safer of the two failure modes.
Retries are the handler's business, and it can schedule one.
