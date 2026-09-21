# Design notes

Written answers for the parts I did not build, plus the question the brief asks
about the workflow engine.

---

## 1. Identity and authorization (required)

The harness has one identity object, `Principal`, built once at the top of a run
and passed down. Everything below it already behaves as though a real identity
system produced it. Here is the system that would.

### Establishing who the user is

The agent never authenticates anybody. It runs *on behalf of* a user whose
identity was established elsewhere, and the thing it holds is a short-lived
assertion of that fact.

For a manufacturer this is almost always Entra ID or Okta over OIDC. Two
entry paths matter and they differ in one important way:

**Interactive.** The user signs in, the IdP issues an ID token and a refresh
token, and the harness exchanges them for a delegated access token per
downstream system. The user was present, so consent and MFA are real.

**Unattended, which is the interesting one.** A detector fires at 04:00 for a
purchasing manager who is asleep. There is no live session to borrow. The honest
answer is that the *sweep* runs as a service identity holding no data scopes at
all (which is exactly what `principal.SYSTEM` is in this repo), and the moment
it identifies a subject user, it performs an OAuth token exchange
(RFC 8693) against the IdP, presenting its own service credential and the
subject's identifier, and receives a delegated token scoped to that user. The
exchange is what an administrator authorised once, per user, at onboarding; it
is revocable, it is logged at the IdP, and it produces a token that expires in
minutes.

The distinction that matters: the service identity can *ask to act as* a user.
It cannot read a user's mail. Today's `SYSTEM` principal holding
`frozenset()` of scopes is that property, enforced by the type system instead of
by an IdP.

### Getting permissions to the tool layer

Three rules, and the current code already obeys the shape of all three.

**The agent holds no standing credentials.** Not an ERP service account, not an
application mailbox, not a database login. Every downstream call carries a token
minted for one user for one short window. If the agent process is compromised at
rest, the attacker gets code and an empty credential store.

**Scopes come from the token, not from a local table.** `company/users.json`
would disappear. `Principal.scopes` would be populated from the `scp` claim of
the exchanged token, which the IdP derived from group membership. That means
revoking somebody's purchasing authority in the directory takes effect on the
next token, without a sync job and without the harness holding an opinion.

The subtlety worth stating: **the ERP's own permissions are the real boundary,
and the harness's scopes are a second, narrower one.** A delegated token against
SAP or Dynamics already fails if the user cannot create a purchase order. The
harness checks anyway, before dispatch, because a clean refusal naming a rule is
worth far more in an audit log than a 403 from a vendor API. Defence in depth
where the outer layer produces the explanation and the inner layer produces the
guarantee.

**Tokens are fetched per call, never cached across a pause.** This is the rule
the scheduler already enforces. A scheduled task carries `subject_user`, never
scopes and never a token. When it fires on Tuesday, the kernel resolves that
user afresh and the work is gated as them, at that moment, under the permissions
they hold then. Somebody who left the company on Monday has a follow-up that
fails closed, which is the correct behaviour and is not achievable by any design
that stashes a credential in a payload.

### What changes in the code

`Store.principal()` becomes a call to a token broker. `ScopedStore` keeps its
interface exactly. Providers and tools are untouched, because they already
receive a handle rather than credentials, which is the whole reason the
abstraction is shaped this way.

### What I would add that is not here

A **capability audit** at the IdP, not just in the application: the exchange
itself should be logged where the security team already looks, so "what did the
agent do as Dana" is answerable without trusting the agent's own log. And
**step-up authentication** for high-value approvals: above some threshold, the
approval link should force a fresh MFA challenge rather than accepting a
session, because the approval is the one control standing between a model's
proposal and a real purchase order.

---

## 2. Long-term memory (required)

### What gets promoted

Almost nothing, and the restraint is the design.

Two kinds of fact leave a run today (`harness/memory/`):

- `supplier.<id>.slips`: that a supplier moved a promised date, and when
- `part.<id>.last_reroute`: that a part's supply was rerouted, and to whom

Both are **observations of events**, not statements about the current state of
the world. That is the rule I would keep at any scale: *remember what happened,
never what is true*. The ERP knows what the current promised date is. Copying it
into agent memory creates a second answer to a question that already has one,
and the second answer starts rotting the moment it is written.

Applied to the obvious temptations:

| Tempting to remember | Why not |
|---|---|
| "Supplier Z is reliable" | A conclusion, not an observation. Derive it from the slip events at read time |
| "P-4471 usage is 30/day" | The ERP owns this and changes it |
| "Dana prefers Meridian" | A preference inferred from two data points is a stereotype |
| "Production order 4812 starts 9/7" | Mutable, owned elsewhere, and cheap to re-read |

What I would add at scale, and only these: **durable user preferences the user
stated explicitly** ("always ask me before touching anything over £50k"), and
**outcome feedback**: that a recommendation was approved, rejected, or rejected
with a reason. The second is the only honest source of signal about whether the
agent is any good, and it feeds section 5.

### Keeping it accurate

**Every fact carries its age and it is never hidden.** `recall()` returns
`observed_at` and `age_days` on every record and drops anything past
`max_age_days` (90 by default). A memory presented to a model without an age on
it is presented as a timeless truth, which is how agents end up confidently
acting on something that stopped being true in March.

**Memory is never load bearing for a decision.** It enters the context bundle as
`durable_memory`, alongside the live records, and the gate does not read it at
all. The gate re-derives every fact it decides on from the store. So a stale or
poisoned memory can make the agent's *reasoning* worse and cannot make an
unauthorised action possible. That separation is deliberate and I would defend
it hard: the blast radius of a wrong memory should be a worse recommendation,
never a wrong write.

**Events are append-and-count, not overwrite-with-a-conclusion.** The slip
record keeps an occurrence count and the most recent instance. The full history
is in the audit log, which is the thing allowed to grow without bound.

### Avoiding stale beliefs at scale

Three mechanisms I would add:

1. **Invalidation on the source event.** When an ERP change event says a
   supplier's approved-parts list changed, drop every memory keyed to that
   supplier rather than waiting for it to age out. Cheap, and it turns a
   90 day window into a 90 day *ceiling*.
2. **Confidence decay in the prompt, not in the data.** Present older memories
   with explicit hedging rather than deleting them. A nine month old slip is
   weak evidence, not no evidence.
3. **Contradiction as a first-class outcome.** If a provider returns something
   that contradicts a memory, the run should record the contradiction and drop
   the memory, not silently prefer one. This is the case that is easy to skip
   and is the one that actually bites.

---

## 3. Scaling to thousands of employees (required)

### Where it breaks first, in order

**1. The detector sweep is O(detectors × users), synchronous, and in one
process.** This is the first thing to fall over and it is not close. Today
`detect.sweep()` loops every detector over every subscribed user in one pass. At
5,000 employees and a dozen detectors that is 60,000 scoped queries per cycle,
serialised, and the cycle has to finish before the next one starts.

The fix is to invert it. Detectors should be driven by **change events, not by
polling**: an ERP change data capture stream, a Graph webhook on a mailbox, a
calendar subscription. A message on the stream names the entity that changed,
which fans out to the small set of users who care about that entity. Scenario
A's detector becomes "an inbound message from a supplier contact arrived" rather
than "scan every open purchase order for every buyer". The scheduled sweep stays
as a slow safety net for conditions no event announces, running nightly at low
priority.

This is also the change that makes the cost tolerable. A poll that finds nothing
99% of the time still pays for the queries.

**2. SQLite.** One writer. It is genuinely the right choice for this repo and it
is the second thing to go. Postgres, and then the interesting decision: the
harness tables (runs, approvals, workflow instances, schedule, audit) partition
by user, so they shard cleanly by tenant and then by user hash. The company data
does not, because it is not the harness's data at all in production; it lives in
the ERP and the providers become API clients.

Worth being concrete about what replaces what. `ScopedStore` keeps its
interface; behind it, `purchase_orders()` becomes an authenticated call to the
ERP and `documents()` disappears. The append-only audit stays in Postgres with
the same trigger and the same hash chain, because it is the one thing that must
not live in a system the agent could also write to by another route.

**3. The single-process scheduler.** `run_due()` fires everything due, in one
loop, in one process. It needs to become a queue with leases: workers claim a
task with a visibility timeout, the durable state is already in the database, so
this is a change to the claim query rather than to the model. The existing
"mark fired before running" behaviour becomes "lease, run, ack", which is
strictly better and is the version I would have written if concurrency were in
scope.

**4. Cost, which is a scaling limit even when nothing is technically broken.**
Measured, from the committed cassettes on `claude-fable-5`:

| Call | Input | Output |
|---|---|---|
| `plan` (Scenario A) | 4,003 | 1,466 |
| `plan` (Scenario B) | 4,044 | 1,852 |
| `plan.workflow_params` | 4,411 | 310 |
| `workflow.select_supplier` | 131 | 153 |
| `workflow.draft_notification` | 549 | 112 |

Two things fall out of that table. The planner calls dominate, at roughly 4k
input each, and the two bounded workflow steps are trivial by comparison: 131
and 549 input tokens, because they are given a filtered list rather than the
world. And `plan.workflow_params` costs almost as much input as the plan itself,
because it resends the whole bundle to fill six fields.

So the levers, in the order I would pull them:

1. **Fewer items**, via the event-driven detectors above. A poll that finds
   nothing still pays for its queries, and this is the only lever that reduces
   calls rather than the cost of each.
2. **Cache the bundle, not just the system prefix.** Caching is already wired
   on the system block (tool catalogue and rules), which is the stable part.
   The bigger win is that `plan` and `plan.workflow_params` share a ~4k bundle
   back to back, and the second call currently pays full price for it. Moving
   the bundle ahead of the last cache breakpoint would make the parameters call
   nearly free.
3. **A cheaper model for the bounded steps.** `select_supplier` picks from a
   two-item list against an enum schema, and `draft_notification` writes four
   sentences with a deterministic fallback if it gets them wrong. Neither needs
   a frontier model. At these token counts the saving is small in absolute
   terms, which is exactly why it is third: it is the lever people reach for
   first and it is worth the least here.

Judge cost per resolved attention item rather than per call. A cheaper model
that proposes something the gate refuses has not saved anything.

**5. The audit hash chain serialises writes.** Each entry reads the previous
hash, so concurrent runs contend on the tail. At scale I would chain **per run**
rather than globally, with each run's terminal hash anchored into a slower
global chain. Same tamper-evidence property, no global write lock. I would not
do this before it hurts, because a per-run chain is harder to reason about and
the global one is correct.

### What does not break

The parts I would not change: the scoped handle, the tool contract, the gate's
rule structure, the workflow definition format, and the run folder. They are all
per-run and stateless, so they scale by adding processes. That is the payoff for
keeping the kernel free of domain logic, and it is the claim Scenario B
tests. It added a detector, a provider, two tools and a user, and changed nothing in
the kernel, the gate, the planner or the audit layer.

---

## 4. Connecting real systems (optional)

### What does not change

The `ScopedStore` interface, every provider signature, every tool signature, the
gate, the workflow definitions, the audit log, the run folder. That is the point
of the abstraction and it should survive contact with a real API or it was not
worth having.

### What changes

**The ERP.** `ScopedStore.purchase_orders()` becomes an OData or BAPI call
carrying the user's delegated token. Three things get harder and all three are
real:

- *Latency.* Today the context bundle is built from eight local reads. Against a
  real ERP that is eight network calls with tail latency. Providers should fetch
  concurrently, and the bundle needs a budget with partial results recorded in
  `omitted`, which is the mechanism that already exists for scope denials, used
  for a second reason.
- *Pagination and filtering.* `production_orders(consumes=part_id)` filters in
  Python over a handful of rows. Against a real system this must push down into
  the query or it pulls the whole table. Each provider method becomes a query
  the ERP can actually answer.
- *Writes are not idempotent by default.* `create_purchase_order` relies on the
  harness's idempotency ledger. A real ERP may create two orders if called
  twice. The ledger must be written **before** the call, in a separate
  transaction, with the ERP's own idempotency key or external reference field
  carrying our key. This is the single most likely place to create a real
  duplicate purchase order and it deserves the care.

**Microsoft Graph.** `inbox()` maps to `/me/messages` and `my_events()` to
`/me/calendarView`, genuinely close to what is here, because the mail and
calendar providers were written to take no user argument, which is exactly the
`/me` shape. `is_out_of_office()` maps to `/users/{id}/calendar/getSchedule`,
which returns free and busy without event details, which is the reason the
method returns a boolean rather than events. That was not an accident.

Graph adds change notifications, which is the mechanism section 3 depends on:
the Scenario A detector stops polling inboxes and subscribes.

**A document store.** This is a new provider, not a change to an existing one,
and it is the one place I would resist the obvious design. The temptation is a
vector index over everything and a top-k retrieval into the bundle. The problem
is that the gate cannot check a passage the way it checks a supplier record, and
the citation requirement in `plan/schema.py` becomes unverifiable. I would scope
retrieval to documents *attached to the entities in focus*: the spec for this
part, the contract with this supplier. That way every retrieved passage still
has an addressable record id behind it.

---

## 5. Observability and evaluation (optional)

### What I would trace

The audit log is already the trace; it needs a span id and an exporter, not a
redesign. One span per phase, with the run id as the trace id. The attributes
worth having on each:

- **context**: providers used, providers skipped, records returned per system,
  bundle size in tokens, anything omitted and why
- **plan**: model, cache hit rate, input and output tokens, latency, whether a
  cassette was replayed, citation count, confidence
- **gate**: every rule and its verdict, not just the outcome
- **execute**: per step, so tool, idempotency key, whether it was a replay,
  latency, and for compensations whether the effect was actually reversed

The last one is the one people forget. A dashboard that counts "compensations
run" without distinguishing `effect_reversed: true` from `false` will tell you
everything rolled back cleanly when half of it sent emails that cannot be
unsent.

### What "did a good job" means measurably

Split it, because these fail differently and conflating them hides both:

**Detection quality.** Precision is measurable directly: what fraction of
attention items led to an approved action rather than a rejection or a
no-action. Recall is harder and needs a ground truth the agent cannot
produce. I would get it from incidents. Every production stoppage caused by a material
shortage is a recall failure, and comparing the incident log to the attention
item log gives a real miss rate.

**Recommendation quality.** The approval decision is the label, and it arrives
free. Approval rate, rejection rate, and, most informative of all, the
**modification rate**: how often a human approves something different from what was proposed.
A recommendation that is always approved unchanged is either very good or a
rubber stamp, and the two are distinguishable by looking at time-to-decision.

**Execution quality.** Workflow completion rate, compensation rate, and
irreversible-effect-not-reversed count. These should be near zero and any drift
is a bug, not a quality signal.

**Cost per resolved item**, which is the number that decides whether any of this
survives a budget review.

### Catching a regression before users do

The recording layer in `plan/llm.py` is the eval harness, which is why it is in
the main path rather than in `tests/`. Every run records the exact context
window and the exact output.

1. **A golden set from real runs.** Take recorded context bundles where the
   human decision is known, replay them against a new prompt or model, and
   compare. This is a regression suite that grows itself, and because cassettes
   are keyed on the situation rather than on prompt bytes, they survive prompt
   edits.
2. **Assertions the gate already implies.** Every gate rule is a property a good
   plan satisfies. Running the golden set and counting gate refusals gives a
   hard, unambiguous regression signal with no judgement involved: a prompt
   change that makes the model propose more unapproved suppliers shows up as a
   number.
3. **A judge for what is left.** Does the headline state the risk, name the
   cause, and propose an action? Are the citations real record ids that appear
   in the bundle? The second is checkable in code, and I would do it in code
   rather than with a judge, because a mechanical check beats a probabilistic
   one whenever one is available.
4. **Shadow mode for anything user-visible.** Run the new configuration
   alongside the old on live attention items, surface only the old one, and
   compare proposals. Divergence is the thing to alert on.

The honest gap: none of this catches a *systematically* bad recommendation that
humans also approve. For that the only real signal is outcome: did the part
arrive, did the line run. It arrives days later, and it is the reason the
follow-up loop exists at all.

---

## 6. Would I build the workflow engine this way again?

The brief asks: if you were designing a deterministic workflow engine from the
start, where reasoning is a small node inside the graph rather than the thing
that drives it, would you still build it the way you did here?

**Mostly yes, and I think the core decision is right.** Three things I would
keep without hesitation:

- The definition is an **immutable tuple walked by index**, and the plan schema
  refuses to carry both a workflow and free-form actions. Together these make
  "the model cannot reorder, skip or add a step" a property of the parser rather
  than a promise in a prompt. That is the strongest version of the guarantee I
  know how to build.
- **Model steps are bounded by construction**: code computes the candidate set,
  the model picks from it, and the answer is validated against the set. A model
  step that could return something the code did not offer is not bounded, it is
  just supervised.
- **Idempotency keyed on instance and step**, not on arguments. This is what
  makes resumption exact rather than probable, and it is a one-line decision
  that removes an entire class of duplicate-write bug.

**Three things I would do differently.**

**1. Steps should declare their reads and writes, not just their tool.** Right
now a step is a Python callable and the engine knows only which tool it names.
That is enough to compute scopes up front, and not enough to answer "can this
step run in parallel with that one", or "which steps does a change to the
supplier record invalidate". I would make a step declare `reads` and `writes` as
entity patterns. That buys static validation of the whole graph before it runs,
a real dependency order instead of a hard-coded sequence, and safe parallelism
where the declarations do not overlap. The reroute happens to be strictly
linear, which let me get away with a list; the next workflow will not be.

**2. Compensation should be a property of the transaction, not of the tool.**
Today each tool carries one compensation. But the right way to undo
`create_purchase_order` depends on how far the workflow got: before the supplier
was notified, cancelling is clean; after, it needs a retraction too. A single
compensation per tool cannot express that. I would attach compensation to the
*step*, with the tool providing a default, and let a step override it with
knowledge of its position in the graph.

**3. The human gate should be a node type, not a phase outside the graph.**
This is the one I would change first. Right now approval happens in the kernel,
before the workflow starts, and the whole workflow is gated at once against the
worst case. That works for the reroute and it does not generalise: a workflow
that needs a second approval halfway through, say if the chosen supplier turns
out to be more expensive than the estimate, cannot express it. Approval should
be an `await_human` node that suspends the instance and persists it, with the
existing approval routing hanging off it. The machinery is already nearly there:
instances persist after every step, and resumption works. It is a node type and
a status, not a rewrite.

**The deeper question underneath it.** The brief's framing, reasoning as a
small node inside the graph rather than the thing driving it, is one I agree
with, and I would go further: the free-form planner should shrink over time, not
grow. Every time a situation recurs often enough to have a right answer, that
answer should become a workflow, and the planner's job narrows to routing.
Scenario A has a workflow because purchasing already knew the steps. Scenario B
does not, because nobody has decided yet. The healthy end state is that the
planner mostly picks a declared workflow and supplies its parameters, and
free-form execution is the exception you look at in the logs and ask whether it
should have been a workflow.

Which means the metric I would actually watch, and which this repo does not yet
produce, is **the proportion of executed actions that ran inside a declared
workflow**. If it goes up, the system is learning what the business actually
wants. If it goes down, the agent is improvising more and somebody should ask
why.

---

## 7. Version migration for in-flight instances

Not required to implement, and the brief asks for the design.

An instance records the definition version it started under and refuses to
resume against a different one (`WorkflowVersionMismatch`, tested). Refusing is
the floor, not the answer.

The real answer depends on what changed, and I would make that explicit in the
definition rather than inferring it:

- **A step's implementation changed but its contract did not** (better prompt,
  bug fix). Safe to resume on the new version. Mark the change `compatible` and
  let in-flight instances pick it up.
- **A step was added, removed, or reordered.** In-flight instances must finish
  on the old definition. This means keeping old versions loadable, which means
  definitions are versioned artifacts rather than whatever is currently in the
  module. The change I would make to support this is to register every version
  under `name@version` and have instances resolve by that key.
- **A step's parameters changed shape.** The instance's persisted params no
  longer validate. Either supply a migration function alongside the new version
  or fail the instance loudly and compensate. Silently coercing is how you get a
  purchase order for the wrong quantity.

For anything that cannot resume, the safe default is **compensate and re-enter
the loop** rather than abandon: the situation that triggered the workflow is
probably still true, and re-detecting it produces a fresh plan against the
current definition. That path already exists: it is what the follow-up check
does when a shipment has not arrived.

---

## 8. What only a real model call found

Everything below the planner is deterministic, so the harness was fully working
against a scripted client before a single live call was made. That was the
right order to build in and it hid four defects, each of which is worth more
than the feature it broke.

**A schema that cannot hold the answer looks like a disobedient model.** The
plan's `workflow_params` was typed `dict`, which Pydantic renders as
`{"type": "object"}` with no properties. Constrained decoding against that can
emit `{}` and nothing else. The model chose `po_reroute` correctly and returned
empty parameters, twice, including on a retry that handed it the validation
error verbatim. It read like an instruction-following failure and it was a
schema bug. `ProposedAction.params` had the identical flaw, found separately a
few minutes later, which is the more useful half of the story: the same mistake
in two places, because the fix for the first one was applied to the first one
rather than to the pattern.

The general rule I would take to any project: **when a model persistently
leaves a field empty, read the JSON schema it is decoding against before
rewriting the prompt.** Open containers are not fillable. Workflow parameters
are now a second call against the workflow's own concrete model; free-form
actions are a tagged union with one variant per tool the user may run, so the
arguments are real properties and a tool outside the catalogue is unnameable.

**A prose instruction is not a constraint.** Asked, clearly, to return a
supplier id from an offered list, a live call returned `"S-Z Meridian Drives"`.
That is a reasonable answer to the question the model thought it was asked, and
it is outside the permitted set. Saying it more firmly would have made it rarer
and not impossible. The candidate list is now an enum in the request schema, so
the bound is structural, with the step's own check kept underneath because the
point of defence in depth is not finding out the top layer failed by having a
purchase order appear.

**A validation ceiling picked by guesswork discards good work.** The headline
had `max_length=400`, a number chosen by imagining how long "one short
paragraph" is. A real headline naming two dates, a part, an order and a
supplier is longer than that, and a correct plan was thrown away validating it.
Limits on generative fields should be set from observed output, and a schema
violation should cost one retry rather than the whole run.

**Encoding bugs hide in the one file nobody reads.** `.env` was read as
`utf-8`. Windows PowerShell writes a BOM, so the first variable parsed as
`\ufeffANTHROPIC_API_KEY`, and the harness reported no API key while the file
plainly contained one.

None of these were reachable from the scripted client, because a scripted
client returns what the author already believes the model will return. That is
exactly what makes it useful for testing the gate and useless for testing the
boundary. The cassettes are the compromise: recorded from real calls, replayed
deterministically, so the tests stay fast and the thing they replay actually
happened.

One consequence worth noting for the evaluation section above. The live model
chose to reroute 120 units, the shortfall at the production order's start,
rather than the full 400 on the original order. That made `amend_original_po`
take its *reduce* branch instead of *cancel*, a path the scripted client never
exercised and which the audit log shows working: `PO-77812` went from 400 units
to 280 rather than being cancelled outright. It is a better answer than the one
I scripted, and I would not have known to write the test for it.
