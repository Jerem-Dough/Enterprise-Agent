# Harmony Harness: where things are

An extendable agent harness for enterprise work. This file routes; it holds no
content. Every folder states its own purpose in its `CONTEXT.md`.

## Where do I go for…

| Task | Go to |
|---|---|
| Run the whole story | `python harmony.py demo`, or `demo.py` |
| Understand the loop | `harness/CONTEXT.md`, then `harness/kernel.py` |
| Understand the modelled world | `MODEL.md`, then `company/CONTEXT.md` |
| Add a provider, tool, detector or workflow | `README.md`, "Extending it" |
| Change what the agent may do | `harness/gate/CONTEXT.md` |
| Change a threshold or a rule | `company/policy.json` |
| Change who may do what | `company/users.json` |
| Understand a past run | `runs/<run-id>/`, or `python harmony.py audit <run-id>` |
| The parts I did not build | `docs/DESIGN.md` |
| See every extension point | `python harmony.py catalogue` |

## The shape

```
detect ─→ gather ─→ plan ─→ gate ─→ [human] ─→ execute ─→ follow up
```

`company/` is the factory: the seeded world, stable across runs.
`runs/` is the product: one readable folder per run, new every time.
`harness/` is the machinery, and knows nothing about purchasing or quality.

## Standing rules

**The gate does not trust the plan.** It re-reads every fact it decides on from
the store. Changing this removes the only real guarantee in the system.

**A provider or a tool gets a `ScopedStore` and nothing else.** There is no
method that widens it back. The gate, the kernel, the scheduler and the audit
hold the privileged handle and are the trusted computing base.

**Never display or calculate a value that is not real.** No placeholder metrics,
no invented dates, no estimated quantities where a record gives an exact one. If
something cannot be read, say so: providers record a denial in `omitted` rather
than reasoning around the gap.

**Compensation reports what it actually achieved.** Reversing a record change is
not the same as unsending an email. Return `effect_reversed: False` and say why.

**The audit log is append only and nothing may make it otherwise.** It is the
deliverable. Everything else can be rebuilt from `company/`.

**No em dashes and no double hyphens** anywhere a person will read: seeded data,
messages the agent sends, CLI output, docs, comments. Use a full stop when the
two halves are whole thoughts, a comma for an aside, a colon when the second
half defines the first. Do not fix these with find and replace; read the clause
and pick the punctuation it needs.

Where the text is generated, the rule is enforced by rejection, not by
rewriting. The workflow's notification step checks the draft and falls back to
its deterministic template, because substituting punctuation blindly produces
sentences like "deliberately quiet. a ledger, a queue". Be honest about the
limit: for a free-form action, where there is no template to fall back to, the
rule is prompt-level only. Failing a real supply notification over a dash is
the wrong trade, and pretending the guarantee is total would be worse.

## Conventions

Python 3.13, SQLite, Pydantic for anything a model produces or a tool accepts.
Registries are a module in a folder plus one line in `_load()`. Docstrings carry
the why, not the what; the signature already says what.
