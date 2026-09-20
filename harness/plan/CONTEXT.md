# plan/: the only open-ended reasoning in the system

One job: attention item plus context, to a validated proposal. Writes nothing.

## Inputs
- Working (this run): the attention item, the context bundle
- Reference (every run): the tool catalogue narrowed to this user's scopes, and
  the workflow catalogue narrowed to workflows this user could complete

## Process
1. Build the system prompt (stable per user, and cached) and the user prompt.
2. Call the model with `output_format=Plan`, so the response is a validated
   object rather than prose somebody has to parse.
3. Record the exact prompt and the exact response.

## Outputs
- A `Plan`, written to `runs/<run-id>/04-plan.json`
- The context window, written to `runs/<run-id>/03-prompt.json`
- A cassette under `cassettes/`

## Human check
Open `03-prompt.json`. Everything the model was shown is in it. If a tool the
user cannot run appears there, the catalogue narrowing has broken.

## What it may and may not decide
May: whether to act at all, which declared workflow covers the situation, what
parameters it gets, which tools to call when no workflow covers it, and what to
say to the person.

May not: whether it is permitted (the gate), whether a workflow's steps may be
reordered (the definition), whether a write happens without approval (the
kernel), or what any record actually says (the providers).

## Why every claim carries a citation
`Plan.citations` ties each factual assertion to a system and a record id. A
recommendation the reader cannot check is a guess with good grammar. The
citation requirement makes checking mechanical rather than a matter of trust.
