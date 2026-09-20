# What I modelled, and why

Northfield Manufacturing: five people, four parts, five suppliers, four purchase
orders, three production orders, four lots, six emails, five calendar events.
Everything lives in `company/` as readable JSON and loads into SQLite on `init`.

The size is a decision, not a shortcut. The brief asks for a handful of entities
and four tools that make the story real rather than a fake ERP with forty
tables, so the test I applied to every field was: *does a scenario or a rule
read this?* Fields nothing reads were cut, including several from the sample.

---

## Kept from the sample, unchanged

`parts`, `suppliers`, `purchase_orders`, `production_orders`, `quality_lots`,
mail, calendar, users and the clock all keep the sample's shape and field names.
The specific records in the brief are kept verbatim where they carry the
scenario: `P-4471`, `PO-77812` with Supplier Y, production order `4812`, lot
`L-2093`, message `M-001`, Dana's out of office, and `today = 2026-09-02`.

I checked the sample's dates against the real calendar and they hold up:
2026-09-02 is a Wednesday, 2026-09-08 is the Tuesday the supplier's email names,
and `P-4471` at 150 on hand against 30 a day runs to zero on 2026-09-07, the day
production order 4812 starts. The scenario is arithmetically real, so the demo
does not need a thumb on the scale.

---

## Added

**Noise, in specific shapes.** The brief asks for irrelevant emails, unrelated
POs, and a supplier that looks attractive and should not be used. Each piece of
noise here is aimed at a particular failure:

| Record | What it is | What it would break |
|---|---|---|
| `S-Q` Apex Rapid Components | Approved supplier, lowest price on file for `P-4471`, one day lead time, not approved *for this part* | A check that reads `approved` and stops |
| `M-004` | Apex emailing Dana an unsolicited quote the morning the scenario runs | A planner that takes the cheapest offer in the inbox |
| `S-W` Foundry Line Supply | Approved *for the part*, nine day lead time | A workflow that checks approval but not the date |
| `L-2088` | Released lot of the right part, too small and already committed | A lot search filtering only on status |
| `M-005` | Addressed only to the quality manager | A mail provider that takes a user argument |
| `M-002`, `M-003`, `M-006`, `PO-77790`, `PO-77801`, `4805` | Genuinely irrelevant | Nothing. They are there so the signal has to be found |

`S-Q` deserves the extra sentence. It holds an open order (`PO-77801`) and it
writes to Dana, so it satisfies the first half of the Scenario A detector's
condition exactly as Kestrel does. It does not become an attention item because
the part it supplies is nowhere near short. That is the detector's second
condition earning its keep against a real near-miss rather than a strawman.

**A fifth user and a second role.** `u-102` Marcus Webb is Dana's designated
backup, and he holds a *lower* approval limit than Dana and no `erp:po:cancel`.
The sample implies a backup exists; making their authority different is what
turns "route to the backup" from a pointer swap into a decision the code has to
re-check. `u-202` Elena Ortiz is Scenario B's quality manager, with no purchase
order scopes at all, which is why her shortage path has to hand off to
purchasing rather than order the part herself.

**`policy.json`.** The sample puts `approval_limits` on the user, which is right
for a personal limit. Rules that belong to the company rather than to a person
needed somewhere to live: the end of day hour, whether every write needs a
human, the price premium ceiling, and the lot eligibility rules. They are data
so that a reviewer can read the rules without reading the gate, and so that
changing a threshold is not a code change.

**`allocated_lots` on production order components.** The sample's `quality_lots`
point at production orders via `allocated_to`, but the production order has no
way back. Scenario B reallocates, which means writing both ends, so the
component carries the lots allocated to it. Both directions are updated in one
call and one transaction.

**`_seed_note` fields.** A few seed records carry an underscore-prefixed note
explaining the trap they set, for a human reading `company/`. The loader strips
every key beginning with `_`, so nothing reaches the model or the rules. This is
documentation living next to the thing it documents rather than in a wiki.

---

## Changed

**`S-Y` lead time is 6 days, and `promised_date` stayed at 2026-09-04.** The
sample gives Supplier Z a two day lead time but says nothing about Y's. Six days
makes Kestrel's slip to the 8th consistent with a real re-plan rather than an
arbitrary number.

**`daily_usage` is reported two ways.** `days_of_cover` nets off safety stock
(4.33 days) and is what the detector triggers on; `days_to_empty` is the raw
shelf count (5.0 days) and is what the brief means by "run out in five days".
Both are in the context bundle because they differ, and a recommendation citing
one while the reader is thinking of the other reads as an error even when the
arithmetic is right.

**Mail is a store table, not an append-only log.** Sent messages land in the
same table as received ones, so a notification the agent sent is visible to the
recipient's provider. That is what makes the compensation for `notify_production`
demonstrable: you can see both the notice and its retraction.

---

## Left out

**Anything not read by a scenario or a rule.** No cost centres, no warehouses,
no bills of material beyond the component list, no supplier contracts, no
receipts or goods-received notes, no invoice or payment records, no routings or
work centres. Each would be real in a manufacturer and none is load bearing
here.

**Attachments, threads and recipients beyond `to`.** No `cc`, no `bcc`, no
`in_reply_to`. The mail provider matches on focus tokens and sender address,
neither of which needs a thread model.

**Calendar detail beyond free and busy for colleagues.** A user reads their own
events in full. For anybody else, `is_out_of_office(user, day)` returns a
boolean. The approval routing rule needs to know somebody is away; it does not
need to know they are at a supplier site visit, and exposing the title would be
a small leak for no gain.

**Multi-tenancy.** One company. Every row would carry a tenant id in a real
deployment and every read would be scoped by it; the mechanism would be the same
one the `ScopedStore` already implements, one level up.

---

## Two deliberate deviations from the brief

**1. The follow-up is scheduled against the replacement, not against Tuesday.**

The brief says the agent "schedules a check for Tuesday to confirm the new
shipment actually arrived". In the brief's own numbers, Tuesday 9/8 is when
*Supplier Y* said their delayed shipment would land. The replacement from
Supplier Z has a two day lead time, so it is promised 9/4. Scheduling the check
for 9/8 would mean checking the right order on the wrong supplier's date, and
missing four days in which the replacement could have failed to arrive.

So `schedule_arrival_check` queues the check for the replacement's promised
date. On a miss it raises an attention item and queues another check for the
next working day. In the seeded world that is 9/4, then 9/7, then Tuesday 9/8,
so the clock still advances to Tuesday and a follow-up still fires there, on a
check that is about the right shipment.

The dedupe key for a missed arrival is the order, not the day. One late pallet
is one unresolved situation across all three checks, not three. Keying on the
date would page somebody every morning about the same problem, which is how
agents get muted.

**2. The reroute workflow has seven steps, not six.**

Purchasing's list begins "confirm the alternate supplier is approved for the
part", which assumes a supplier already in hand. Something has to pick it. I put
that choice in the definition as step one rather than leaving it to whatever the
planner happened to put in the parameters, so it is bounded and logged in the
same place as everything else. Purchasing's six steps follow it in their stated
order, unchanged.

---

## The permission model

Scopes are `system:resource:action`. There are eleven, and each one is held by
at least one user and required by at least one provider or tool.

```
erp:part:read          erp:supplier:read      erp:production:read
erp:po:read            erp:po:create          erp:po:cancel
erp:quality:read       erp:quality:reallocate
mail:read              mail:send              calendar:read
production:notify
```

Creating and cancelling a purchase order are separate scopes because they are
separate authorities in a real purchasing department: a buyer may be trusted to
place an order against a contract and not to unwind somebody else's. Marcus has
`erp:po:create` and not `erp:po:cancel`, which is why he can approve a reroute
without being able to execute one himself. Those are different questions and the
harness keeps them apart.

`schedule_follow_up` requires no scopes at all. It reads and writes nothing in
any company system, it puts a row in the harness's own queue, and the work it
eventually wakes up is gated afresh as that user at that moment. Deferred work
carries a user id and never a capability.

---

## Where the model is enforced

Worth stating plainly, because "we have a permission model" usually means a list
of strings nothing checks.

- **Providers** receive a `ScopedStore` and cannot reach past it. Mail and
  calendar reads filter on the principal the handle holds rather than on an
  argument, so there is no call that reaches another person's inbox.
- **Tools** declare their scopes; the runner checks them before dispatch and
  the store checks again on the way to the data.
- **The gate** re-reads every fact it decides on from the store rather than
  trusting the plan's account of the world.
- **The audit log** is append only by database trigger for every connection,
  including the privileged one, and hash chained so a removed or altered row
  breaks verification at a named sequence number.

`tests/test_gate.py` asserts the refusals *and* that the world is unchanged
afterwards. A gate test that checks only a boolean is checking that a function
returns, not that a purchase order failed to exist.
