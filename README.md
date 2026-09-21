# Harmony Harness

An extendable agent harness for enterprise work. Every employee gets an agent
that notices things in their own systems, reasons about them, proposes an
action, and executes it only with a human's approval and a full record of why.

Built for the Scenario A brief: a supplier slips a delivery, a production order
is about to miss its start, and the agent has to notice, reason, ask, act, and
come back on the promised date to check.

---

## Run it

```bash
python -m venv .venv
.venv/Scripts/activate          # or source .venv/bin/activate
pip install -r requirements.txt

python harmony.py demo
```

One command, no API key required. It runs Scenario A from an unprompted
detection through approval and execution, advances the clock to Tuesday with
the follow-up firing, runs Scenario B, walks six failure cases, and prints the
audit trail reconstructed from the log alone.

It needs no key because every model call the demo makes was recorded against
Claude Opus 5 and committed to `cassettes/`. The default mode replays a
recorded call when one exists. The model is real, the responses are the
model's, and the run is reproducible.

### With a key

To make live calls instead, copy `.env.example` to `.env` and add an
`ANTHROPIC_API_KEY`. Then:

```bash
HARMONY_LLM_MODE=record python harmony.py demo    # re-record the cassettes
HARMONY_LLM_MODE=live   python harmony.py demo    # call the API, keep the cassettes
```

`HARMONY_LLM_MODE` is `auto` (replay if recorded, else call and record; the
default), `replay` (never call; a missing cassette is an error), `record`, or
`live`. The model is called in four places: the planner, a second call that
fills the chosen workflow's parameters against its own schema, and two bounded
steps inside the reroute workflow.

`python harmony.py demo --scripted` runs the same story against a scripted
client with canned answers, including deliberately wrong ones. It exists for
the failure cases and for CI, not as a substitute for the model.

### Step by step

```bash
python harmony.py init --fresh                 # seed the world
python harmony.py detect                       # run the detector sweep
python harmony.py run --all                    # gather, plan, gate, ask
python harmony.py approvals                    # what is waiting, and with whom
python harmony.py clock --to 2026-09-02T17:00  # end of day
python harmony.py tick                         # reroute stale approvals, fire due work
python harmony.py approve apr-xxxx --as u-102
python harmony.py execute apr-xxxx
python harmony.py audit run-xxxx               # the transcript
python harmony.py verify                       # check the hash chain
python harmony.py catalogue                    # every extension point
```

### Tests

```bash
python -m pytest
```
82 tests against a real database. Covering the gate, trigger dedupe and workflow
resumption as the brief requires, plus approval routing and the audit log.

---

## How it fits together

```
detect ─→ gather ─→ plan ─→ gate ─→ [human] ─→ execute ─→ follow up
   │         │        │       │                    │          │
detectors  providers  LLM   in code            tools /     scheduler
                                              workflows
```

A run stops at the approval boundary and returns. It does not block or poll, so
the pause survives a restart: the pending state is a row, not a stack frame.

| Package | One job |
|---|---|
| `harness/clock.py` | The only source of "now". Advanceable, persisted |
| `harness/principal.py` | Who the agent is acting as, resolved once per run |
| `harness/store.py` | Durable state. `ScopedStore` is all a provider or tool can touch |
| `harness/audit.py` | Append only, hash chained, the thing requirement 7 is about |
| `harness/runlog.py` | One readable folder per run |
| `harness/providers/` | Context per system, scoped to the user |
| `harness/detect/` | Attention items on a schedule, deduped on the situation |
| `harness/plan/` | The only open-ended reasoning. Produces a validated proposal |
| `harness/gate/` | Permissions, policy, approval routing. Enforced in code |
| `harness/tools/` | Typed, scoped, idempotent, reversible actions |
| `harness/workflows/` | Declared graphs. The definition decides the order |
| `harness/schedule/` | Deferred work that survives a restart |
| `harness/memory/` | What outlives a run, and how old it is |
| `harness/kernel.py` | The loop, and nothing domain-shaped |

Each package carries a `CONTEXT.md` stating what it reads, what it does, what it
writes, and what a human should check. Reading those top to bottom explains the
system without running anything.

---

## Extending it

Every extension point is a module in a folder plus one line in a `_load()`
function. None of them requires touching the kernel.

### Add a provider

```python
# harness/providers/documents.py
from . import ProviderResult, provider

@provider("documents",
          description="Specifications and drawings for a part.",
          scopes=["docs:read"])
def gather(store, clock, focus):
    result = ProviderResult(system="documents")
    result.records["specs"] = store.specs(focus["part_id"])
    return result
```

Add `documents` to `_load()` in `harness/providers/__init__.py`. A provider
whose scopes the principal lacks is skipped and the skip is audited. A scope
denial inside one is recorded in `omitted` rather than raised, so the plan sees
what the user could not.

### Add a tool

```python
# harness/tools/expedite.py
from pydantic import BaseModel, Field
from . import ToolContext, tool

class ExpediteParams(BaseModel):
    po_id: str
    reason: str = Field(min_length=10)

def _compensate(context, params, output):
    context.store.amend_purchase_order(params.po_id, output["before"])
    return {"compensated": True, "effect_reversed": True}

@tool("expedite_order",
      description="Ask a supplier to pull a promised date forward.",
      scopes=["erp:po:cancel", "mail:send"],
      params=ExpediteParams,
      compensate=_compensate,
      compensation_note="Restores the promised date.")
def expedite(context: ToolContext, params: ExpediteParams) -> dict:
    ...
```

Add it to `_load()`. It appears in the catalogue, is offered to the planner only
if the user holds its scopes, and gets idempotency and audit for free.

Write the compensation honestly. If the effect cannot be undone, return
`effect_reversed: False` and say why, as `notify_production` does. An engine
that believed a notification had been erased would be lying in a log whose whole
value is that it does not.

### Add a detector

```python
# harness/detect/price_spike.py
from . import AttentionItem, detector, ref

@detector("supplier_price_spike",
          description="A quote has moved more than the contract allows.",
          roles=["Purchasing Manager"],
          scopes=["erp:supplier:read", "erp:po:read"])
def detect(store, clock):
    for supplier in store.suppliers():
        ...
        yield AttentionItem(
            detector="supplier_price_spike",
            subject_user=store.principal.user_id,
            dedupe_key=f"price_spike:{supplier_id}:{part_id}:{quoted_price}",
            summary="...",
            focus={"part_id": part_id, "supplier_id": supplier_id},
            evidence=[ref("erp", "supplier", supplier_id, "quote on file")],
        )
```

Detectors run as the employee, never as the scheduler, and must be
deterministic. Build the dedupe key from the facts that would make this a
genuinely new problem. The price is in the key above because a *different* spike
is a different situation; a re-run of the same sweep is not.

### Add a workflow

Declare steps as an immutable tuple. Code steps return a `StepOutcome`; tool
steps go through `tool_step()`, which wires the idempotency key and the
compensation payload.

```python
DEFINITION = register(WorkflowDefinition(
    name="expedite_then_reroute",
    version="1.0.0",
    description="Ask first, reroute only if the supplier says no.",
    params_model=ExpediteParams,
    gate_facts=_gate_facts,
    steps=(
        Step("ask_supplier", "Request a pull-in.", "tool", _ask, tool="expedite_order"),
        Step("await_reply", "Check for an answer.", "check", _check_reply),
        ...
    ),
))
```

`gate_facts` must derive every policy-relevant fact from the parameters alone,
because the whole workflow is gated before its first step writes anything.

For a model step: compute the candidate set in code, pass it to the model, and
validate the answer against the set. See `select_alternate_supplier` in
`harness/workflows/po_reroute.py`. If the model can name something the code did
not offer, the step is not bounded.

---

## What I cut, and why

**No UI.** The brief says a CLI is fine. What matters is that a human decision
is a separate, recorded, authenticated event, and `harmony.py approve --as u-102`
is that. A web form would have been presentation over the same rows.

**No real identity.** There is no SSO, no token exchange, no credential broker.
`Principal` is the shape a token exchange would produce and everything below it
already behaves as if one existed. How the real version works is the first
required section of the design doc, which seemed a better use of the hours than
a login page nobody would log into.

**No multi-tenancy.** One company. Scoping in a real deployment adds a tenant
dimension to the same mechanism the `ScopedStore` already implements.

**No retry or backoff.** A failed tool call fails its step and the workflow
compensates. Real transient failures need retry with a budget, and getting that
right interacts with idempotency in ways worth doing properly rather than
sketching.

**No vector search over internal knowledge.** The brief mentions reasoning
across "internal knowledge". Both scenarios are answerable from structured
records and one email, so a retrieval layer would have been scaffolding built to
be pointed at rather than used.

**No compensation on the free-form path.** Compensation needs a declared order
to unwind. A free-form list has no contract about what its earlier actions
meant, so execution stops at the first failure and the audit log says how far it
got. Work that needs unwinding is work that should have been a workflow.

**No holiday calendar.** The follow-up re-checks on the next weekday. Real
holidays would come from a real calendar rather than a table invented for a
seeded demo.

**Version migration for in-flight workflow instances.** An instance records the
version it started under and refuses to resume against a different one. The
brief says handling this is a design-doc question; refusing loudly is the honest
floor and silence would be the dangerous alternative.

---

## Requirements checklist

Every item in the brief, and where it is satisfied.

| The brief asks for | Where |
|---|---|
| Detect without being prompted, on a schedule or event | `harness/detect/`, run by `harmony.py detect` and the first beat of the demo |
| Context from ERP, inbox and calendar via distinct providers, scoped to the user | `harness/providers/erp.py`, `mail.py`, `calendar.py`, plus `quality.py` for Scenario B; all read through `ScopedStore` |
| Reason to a recommendation and a proposed plan | `harness/plan/`, real model calls, recorded to `cassettes/` |
| Gate: permissions, policy (PO thresholds, backup approver from calendar), human approval before any write | `harness/gate/` and `harness/gate/approvals.py`, enforced in code |
| Execute through defined tools, idempotently, each step and rationale logged | `harness/tools/`, one runner, one idempotency ledger, one audit entry per call |
| Follow up: a scheduled task that re-checks on Tuesday and re-enters the loop | `harness/schedule/`, `schedule_follow_up` tool, `Harness.tick()`; demo chapter 5 |
| Explain: reconstruct everything from the audit log alone | `harness/audit.py`; `harmony.py audit <run-id>`; `test_audit.py::test_the_log_alone_answers_all_five_questions` |
| Part 2: the reroute as a declared workflow, fixed order, bounded model steps, idempotent, compensating, resumable, versioned | `harness/workflows/po_reroute.py`; `test_workflow.py` |
| Part 3: Scenario B with new quality data, detector, provider, tool, user with different scopes | `harness/detect/lot_hold.py`, `providers/quality.py`, `tools/quality.py`, `u-202`; kernel unchanged |
| Systems and data with noise, a permission model, a tool catalogue, an advanceable clock | `company/`, `MODEL.md`; `harness/clock.py` |
| A real LLM API | Claude Opus 5 through the official SDK, structured outputs |
| `MODEL.md` | [`MODEL.md`](MODEL.md) |
| README: how to run, how to add a tool / provider / detector / workflow, what was cut | This file |
| Design doc: identity and authorization, long-term memory, scaling (required); connecting real systems, observability and evaluation (optional) | [`docs/DESIGN.md`](docs/DESIGN.md), all five, plus the workflow-engine question |
| Tests covering the gate, trigger dedupe, workflow resumption at minimum | `tests/test_gate.py`, `test_dedupe.py`, `test_workflow.py`, plus approvals, audit and docs |
| A recorded run of Scenario A: approval prompt, execution, audit trail | [`docs/RECORDED-RUN.md`](docs/RECORDED-RUN.md), generated from a real run |
| One documented command | `python harmony.py demo`, no key needed |
| Tell us what you cut and why | "What I cut, and why", above, and [`DECISIONS.md`](DECISIONS.md) |

---

## Documents

- [`DECISIONS.md`](DECISIONS.md): the calls that were not forced by the brief,
  and why each went the way it did
- [`MODEL.md`](MODEL.md): what I modelled, what I kept, changed, added and left
  out, and two deliberate deviations from the brief
- [`docs/DESIGN.md`](docs/DESIGN.md): identity and authorization, long-term
  memory, scaling, connecting real systems, observability, and whether I would
  build the workflow engine this way again
- [`docs/RECORDED-RUN.md`](docs/RECORDED-RUN.md): a full Scenario A run:
  approval prompt, execution, audit trail
