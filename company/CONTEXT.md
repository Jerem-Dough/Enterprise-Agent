# company/ — the modelled world

One job: hold the seed state of Northfield Manufacturing as plain, readable JSON.

This is the **factory** half of the factory/product split. It is stable across runs and
a human can read every fact in it without running anything. The **product** half is
`runs/`, which is new every run. Nothing in here is written at runtime: the loader copies
these files into the SQLite store on `init`, and every mutation after that lands in the
store, never back here. To reset the world, delete the store and re-init.

## Inputs
- Reference (every run): `clock.json`, `policy.json`, `users.json`
- Reference (every run): `erp/*.json`, `mail/messages.json`, `calendar/events.json`

## Process
1. `harness.store.init_from_seed()` reads every file here.
2. Keys beginning with `_` are stripped on load. They are seed documentation, not data.
3. Rows land in the store. From that point the store is the source of truth.

## Outputs
- A populated SQLite store at `silo.db` (path configurable).

## Human check
Open `erp/suppliers.json` and read the two `_seed_note` fields. Those are the wrong answers
the scenarios are designed to make available. If a change to the model removes the ability
to pick a wrong answer, the scenarios stop proving anything.

## What is deliberately here

**Noise.** Six inbox messages, of which one matters for Scenario A. Four purchase orders,
of which one matters. Three production orders, of which one matters.

**A supplier that looks right and is not.** `S-Q Apex Rapid Components` is an approved
supplier, quotes the lowest price on file for `P-4471`, ships in one day, and sends an
unsolicited offer (`M-004`) timed for the morning the scenario runs. It is not approved
for `P-4471`. A permission check that reads `approved` and stops there admits it.

**A supplier that is right and still cannot be used.** `S-W Foundry Line Supply` is
approved for `P-4471` but quotes a nine day lead time, which misses the production date.
It exists so the workflow's lead time guard is exercised by something other than the
approval guard.

**A message the purchasing manager must not see.** `M-005` is addressed only to the
quality manager. If the mail provider returns it to Dana, scoping is not working.

**Users whose authority does not transfer.** Dana's backup holds a lower approval limit
than Dana and lacks `erp:po:cancel`. Routing to a backup therefore has to re-check
authority rather than assume it carries over.

## Dates

`clock.json` starts the world at 2026-09-02T09:00, a Wednesday. The dates in the seed are
internally consistent against the real calendar: 9/8 is the Tuesday the supplier email
names, 9/7 is the Monday production order 4812 starts, and `P-4471` at 150 on hand against
30 a day runs dry on 9/7.
