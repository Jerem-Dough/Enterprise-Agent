# harness/: the kernel and its replaceable parts

One job: own the order of the phases, and nothing domain-shaped.

`kernel.py` is the loop. It knows that gathering comes before planning and that
approval comes before execution. It knows nothing about suppliers, lots,
purchase orders or quality holds. Everything domain-shaped is registered from a
folder.

## Inputs
- Reference (every run): `../company/`, loaded by `store.init_from_seed`
- Working (this run): the attention item a detector produced

## Process
1. `detect()` sweeps every detector over every subscribed user.
2. `handle(item)` gathers, plans, gates, and stops at the approval boundary.
3. `execute(approval_id)` runs an approved plan, workflow or free-form.
4. `tick()` reroutes stale approvals and fires deferred work.

## Outputs
- Rows in the store, and an `audit_log` entry for every step
- A readable folder per run under `../runs/<run-id>/`

## Human check
Read `kernel.py` top to bottom. If a purchasing concept has appeared in it, then
something that belonged in a detector, a tool or a workflow has leaked upward.

## The parts, and what each is for

| Module | One job |
|---|---|
| `clock.py` | The only source of now. Advanceable, persisted |
| `principal.py` | Who the agent acts as. Built once, passed down |
| `store.py` | Durable state. Two handles, and the difference is the security model |
| `audit.py` | Append only, hash chained |
| `runlog.py` | The glass box: one folder per run |
| `errors.py` | The failures the harness produces on purpose |
| `providers/` | Context per system, scoped |
| `detect/` | Attention items, deduped on the situation |
| `plan/` | The only open-ended reasoning |
| `gate/` | Permissions, policy, approval routing |
| `tools/` | Typed, scoped, idempotent, reversible actions |
| `workflows/` | Declared graphs |
| `schedule/` | Deferred work that survives a restart |
| `memory/` | What outlives a run, and how old it is |

## The one rule that holds all of it together

A provider or a tool receives a `ScopedStore` and can reach nothing else. The
gate, the kernel, the scheduler and the audit hold the privileged handle and are
the trusted computing base. There is no method that widens a scoped handle back
into a privileged one.
