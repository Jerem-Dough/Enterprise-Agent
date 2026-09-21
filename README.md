# Silo

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

cp .env.example .env            # add your ANTHROPIC_API_KEY
python silo.py demo
```

One command. It runs Scenario A from an unprompted detection through approval
and execution, advances the clock to Tuesday with the follow-up firing, runs
Scenario B, walks six failure cases, and prints the audit trail reconstructed
from the log alone.

`python silo.py demo --scripted` runs the same story with a scripted model
client and no API key, which is what CI uses.

### Without a key

The model is called in four places: the planner, a second call that fills the
chosen workflow's parameters against its own schema, and two bounded steps
inside the reroute workflow. Every call is recorded to `cassettes/` and
replayed on later runs, so a repo with cassettes committed runs the full demo
offline:

```bash
SILO_LLM_MODE=replay python silo.py demo
```

`SILO_LLM_MODE` is `auto` (replay if recorded, else call and record), `replay`,
`record`, or `live`.

### Step by step

```bash
python silo.py init --fresh                 # seed the world
python silo.py detect                       # run the detector sweep
python silo.py run --all                    # gather, plan, gate, ask
python silo.py approvals                    # what is waiting, and with whom
python silo.py clock --to 2026-09-02T17:00  # end of day
python silo.py tick                         # reroute stale approvals, fire due work
python silo.py approve apr-xxxx --as u-102
python silo.py execute apr-xxxx
python silo.py audit run-xxxx               # the transcript
python silo.py verify                       # check the hash chain
python silo.py catalogue                    # every extension point
```

### Tests

```bash
python -m pytest
```

69 tests against a real database. Covering the gate, trigger dedupe and workflow
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
is a separate, recorded, authenticated event, and `silo.py approve --as u-102`
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

## Documents

- [`MODEL.md`](MODEL.md): what I modelled, what I kept, changed, added and left
  out, and two deliberate deviations from the brief
- [`docs/DESIGN.md`](docs/DESIGN.md): identity and authorization, long-term
  memory, scaling, connecting real systems, observability, and whether I would
  build the workflow engine this way again
- [`docs/RECORDED-RUN.md`](docs/RECORDED-RUN.md): a full Scenario A run:
  approval prompt, execution, audit trail
