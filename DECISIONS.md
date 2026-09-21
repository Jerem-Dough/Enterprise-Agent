# Decisions, and why

The brief asks for a decomposition you can defend. This is the record of the
calls that were not forced by the requirements, each with the reason it went
the way it did. Where a decision departs from the brief's wording, it says so.

Where to look first:

| Write-up | What it covers |
|---|---|
| [`README.md`](README.md) | How to run, how to extend each registry, the requirements checklist, what was cut |
| [`MODEL.md`](MODEL.md) | What is modelled, kept, changed, added, left out |
| [`docs/DESIGN.md`](docs/DESIGN.md) | The three required sections, both optional ones, the workflow-engine question, and what the live calls found |
| [`docs/RECORDED-RUN.md`](docs/RECORDED-RUN.md) | A full Scenario A run, generated not pasted |
| [`CLAUDE.md`](CLAUDE.md) | Standing rules, and where everything lives |

---

## 1. The model is Claude Opus 5

`harness/plan/llm.py`, `DEFAULT_MODEL`. Every committed cassette was recorded
against it.

The planner is the one place in the system where a model exercises judgement
over a raw context bundle that deliberately contains bait, and that is the
place to spend on capability. Opus 5 sits at $5 input and $25 output per
million tokens. The next tier up, Fable 5, was tried on the same scenarios at
double that price, and produced no difference in outcome on either: same
workflow chosen, same supplier, same quantity reasoning, same handling of the
unapproved supplier's offer. A full demo run measures at roughly $0.18 on
Opus 5.

The two bounded workflow steps could run on a cheaper model. `docs/DESIGN.md`
§3 argues, with measured token counts, that this is the least valuable of the
three cost levers, so it stays on one model for now and the lever is
documented rather than pulled.

## 2. The arrival check is scheduled against the replacement order

The brief says the agent "schedules a check for Tuesday to confirm the new
shipment actually arrived." In the brief's own numbers, Tuesday 9/8 is when the
*original* supplier said their delayed shipment would land. The replacement
from Supplier Z has a two day lead time and is promised 9/4.

Checking the right order on the wrong supplier's date would miss four days in
which the replacement could have failed to arrive. So the workflow schedules
the check for the replacement's promised date, and on a miss it raises an
attention item and re-checks the next working day. In the seeded world that is
9/4, then 9/7, then Tuesday 9/8. The clock still advances to Tuesday and a
follow-up still fires there, on a check that is about the right shipment.

The dedupe key for a missed arrival is the order, not the day, so one late
pallet is one unresolved situation across all three checks rather than three
alerts. Full reasoning in [`MODEL.md`](MODEL.md), "Two deliberate deviations".

## 3. The reroute workflow has seven steps, not six

Purchasing's list begins "confirm the alternate supplier is approved for the
part", which assumes a supplier already in hand. Something has to pick it.
Leaving the choice to whatever the planner put in its parameters would move
the one genuinely open decision in the reroute *outside* the definition, where
it is neither bounded nor logged with the rest.

So selection is step one, inside the definition, constrained three independent
ways: code filters candidates to suppliers approved for this part, that list
becomes an enum in the request schema so the model cannot name anything else,
and the step re-checks the answer while step two re-derives approval from the
supplier record. Purchasing's six steps follow, unchanged and in their stated
order.

## 4. SQLite holds state; the filesystem explains it

The brief allows static files. They were not enough: deferred work and
in-flight workflow instances have to survive a restart, tool invocations need
an atomic idempotency ledger, and the audit log has to be something a bug
cannot rewrite. SQLite gives all three, and the append-only audit is enforced
by database trigger for every connection including the privileged one, with a
hash chain so a removed row breaks verification at a named sequence number.

Every run still leaves a folder of plain files under `runs/<run-id>/`: the
attention item, the context bundle, the exact context window, the plan, each
gate rule, the approval, each step. Nothing in that folder is authoritative.
Delete it and the harness knows everything it knew. What it buys is that a
person can understand a run by opening a directory, which is most of what
anyone wants during an incident.

## 5. The planner is two calls, and actions are a tagged union

Both fell out of the same discovery, documented in `docs/DESIGN.md` §8. An open
`dict` field renders as a JSON schema with no properties, and constrained
decoding against that can only ever produce `{}`. A model that "ignored" the
instruction to fill in workflow parameters was obeying a schema with no room
for the answer.

So the planner's first call decides *which* path, and a second call fills the
chosen workflow's parameters against that workflow's own concrete model.
Free-form actions are a discriminated union with one variant per tool the user
may run, so arguments are real properties, a tool outside the catalogue cannot
be named, and validation happens at generation time rather than at dispatch.

## 6. Generated text that reaches a colleague meets a stated house style

`CLAUDE.md` bans em dashes and double hyphens anywhere a person reads. The
notification step checks the model's draft and, if it breaks the rule, falls
back to a deterministic template rather than rewriting the punctuation, because
blind substitution produces sentences like "deliberately quiet. a ledger, a
queue". `scripts/check_style.py` enforces the rule on the repository in CI and
also catches invisible characters, after a real byte order mark was found
embedded in a source file.

This is a rule the brief does not ask for. It is kept because generated prose
that goes to a supervisor's inbox should meet the same standard as anything
else the company sends, and because the check found a real defect.

## 7. No server-side refusal fallback

Anthropic's current guidance suggests passing a `fallbacks` parameter so a
safety refusal routes to another model. The harness handles
`stop_reason == "refusal"` explicitly by raising, and does not add the beta
header. A purchasing agent reading ERP records and a supplier's email is not a
plausible refusal case, and a refusal that did occur should surface as a
failed run in the audit log rather than be silently retried elsewhere.

## 8. What was not built

Each of these is defensible as-is and each is documented where it belongs:

- The three workflow-engine changes in `docs/DESIGN.md` §6 (steps declaring
  reads and writes, compensation attached to the step, the human gate as a
  node inside the graph). The brief asks this as a design question.
- Version migration for in-flight workflow instances. An instance refuses to
  resume across a version change, loudly and with a test. The brief says the
  handling is a design-doc question.
- The caching improvement in `docs/DESIGN.md` §3. Measured, documented, not
  built, because the brief is not graded on throughput.
- An eval harness. `docs/DESIGN.md` §5 describes how the cassettes become a
  golden set.
- Everything in [`README.md`](README.md), "What I cut, and why".

---

## State

```
82 tests passing          tree clean                 .env untracked
demo --scripted    OK     harness ~6,000 loc         5 cassettes
demo (replay)      OK     13% docstring density      ~$0.18 per live run
audit chain        OK     style check clean
```
