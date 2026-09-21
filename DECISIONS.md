# Decisions for you

Everything the brief asks for is built, tested and committed. What follows is
what I could not decide for you, ordered by whether it blocks submission.

Where to look first:

| Write-up | What it covers |
|---|---|
| [`README.md`](README.md) | How to run, how to extend each registry, **what I cut and why** |
| [`MODEL.md`](MODEL.md) | What I modelled, kept, changed, added, left out, and two deviations from the brief |
| [`docs/DESIGN.md`](docs/DESIGN.md) | The three required sections, both optional ones, the workflow-engine question, and what the live calls found |
| [`docs/RECORDED-RUN.md`](docs/RECORDED-RUN.md) | A full Scenario A run, generated not pasted |
| [`CLAUDE.md`](CLAUDE.md) | Standing rules, and where everything lives |

---

## Blocking: decide before you submit

### 1. Model: Fable 5, or back to Opus 5

You asked for Fable 5 and it is in (`harness/plan/llm.py`, `DEFAULT_MODEL`).
Cassettes are re-recorded against it. I probed both structured-output shapes
the harness uses before switching, and output quality looked equivalent to
Opus 5 on these tasks.

The tradeoff is cost: **Fable 5 is $10/$50 per MTok against Opus 5 at $5/$25**,
so roughly double for the same work. A full demo run measures at about $0.33.

Three options:

- **Keep Fable 5.** Defensible if the pitch is "we used the most capable model
  for judgement work."
- **Back to Opus 5.** One constant, then re-record. Half the cost, and I saw no
  quality difference on these two scenarios.
- **Split.** Fable 5 for the planner, something cheaper for the two bounded
  workflow steps, which are a two-item enum choice and a four-sentence draft
  with a deterministic fallback. `docs/DESIGN.md` §3 argues this is the *least*
  valuable of the three cost levers at these token counts, so it is a real
  option rather than an obvious win.

**My recommendation: keep Fable 5 for submission.** The cost argument matters
at thousands of employees, and the design doc already makes that argument with
measured numbers. Paying $0.33 for the graded run is not the place to optimise.

### 2. Is this going to a remote, and under whose name

The repo is **local only, no remote**. If it needs to go to
GitHub, tell me which account (`RinDig` or `Jerem-Dough`) and public or private,
and I will push it.

### 3. Commit attribution

Every commit ends with `Co-Authored-By: Claude Opus 5 (1M context)`. For a
take-home this is a judgement call and I am not going to make it for you. Some
reviewers read it as honest, some read it as the wrong signal.

Note the trailer says *Opus 5* because that is the model that wrote the code.
The harness now calls *Fable 5*. Those are two different things and the trailer
is accurate, but it will look like an inconsistency to anyone skimming.

Say the word and I will rewrite the history without the trailers, or leave it.

### 4. The name

The project is called **Silo**, in `Work/Harmony/silo`. You already have a
`Torus/silo`. If the two are going to sit near each other, or if "Harmony" is
the intended product name, tell me and I will rename the package and the CLI.

---

## Non-blocking: my call unless you disagree

### 5. The Tuesday deviation, and the seven-step workflow

Both are documented and defended in [`MODEL.md`](MODEL.md), "Two deliberate
deviations from the brief".

- The arrival check is scheduled against **the replacement order's promised
  date**, not the old supplier's Tuesday, because checking the right order on
  the wrong supplier's date misses four days. The clock still advances to
  Tuesday and a follow-up still fires there.
- The reroute workflow has **seven steps, not six**. Purchasing's list begins
  with a supplier already in hand and something has to pick it, so selection is
  step one inside the definition where it is bounded and logged.

Read those two sections. If you disagree with either, they are contained
changes.

### 6. The em dash rule

I imported your Torus no-em-dash rule into `CLAUDE.md`, then had to clean about
forty violations out of my own prose. It now costs a CI step
(`scripts/check_style.py`) and a fallback branch in the notification step.

It is genuinely your house style and it caught a real bug, so I would keep it.
But I added it on my own initiative, and it is not a requirement of this brief.
Drop it and two files get simpler.

### 7. Refusal fallbacks

Anthropic's current guidance is to pass the server-side `fallbacks` parameter
by default on Fable-tier models, so a safety refusal routes to another model
instead of failing. I did not add it: it needs a beta header and the beta
messages endpoint, and the harness already handles `stop_reason == "refusal"`
explicitly by raising.

For a purchasing agent reading ERP records, a refusal is close to impossible.
I would leave it out and say so if asked. Tell me if you want it in.

---

## Things I chose not to build, and why

Each of these is defensible as-is, and each is an hour or two if you want it.

- **The caching win in `docs/DESIGN.md` §3, lever 2.** `plan` and
  `plan.workflow_params` send the same ~4k bundle back to back, and the second
  call pays full price. Moving the bundle ahead of the last cache breakpoint
  would make it nearly free. I documented it rather than built it, because it
  is an optimisation and the brief is not graded on throughput.
- **The three workflow-engine changes** in `docs/DESIGN.md` §6: steps declaring
  reads and writes, compensation attached to the step rather than the tool, and
  the human gate as a node inside the graph. The brief explicitly asks this as
  a design question, so describing them is the requested answer.
- **An eval harness.** `docs/DESIGN.md` §5 describes how the cassettes become a
  golden set. Building it would be inventing scope the brief does not ask for.
- **Version migration for in-flight workflow instances.** The brief says this
  is a design-doc question. An instance refuses to resume across a version
  change, loudly and with a test.

---

## State

```
77 tests passing          tree clean, no remote        .env untracked
demo --scripted    OK     harness 6,073 loc           5 cassettes
demo (replay)      OK     13% docstring density       ~$0.33 per live run
audit chain        OK     style check clean
```

Every required and optional item in the brief is complete: Parts 1, 2 and 3,
`MODEL.md`, README, the design doc with both optional sections, tests covering
the gate, trigger dedupe and workflow resumption, and a recorded run.
