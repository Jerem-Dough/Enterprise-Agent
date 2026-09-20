# gate/: permissions, policy, and who has to say yes

One job: decide whether a plan may run, and who must approve it. In code.

## Inputs
- Working (this run): the parsed `Plan`, and the principal
- Reference (every run): `company/policy.json`, the store, the clock

## Process
1. Derive what the plan would actually do: required scopes, purchase intents.
2. Run every permission rule and every policy rule. No short-circuiting.
3. Decide the approval requirement and the initial approver.
4. Record the whole decision, rule by rule.

## Outputs
- A `GateDecision`, written to `runs/<run-id>/05-gate.json`
- A row in `approvals` when a human is needed
- One `gate.allowed` or `gate.refused` audit entry carrying every check

## Human check
Read the `checks` array of any `05-gate.json`. Each entry should name a rule, a
verdict, a message a person could act on, and the values it decided on. If a
verdict has no detail, the rule is unauditable.

## The rule this module exists for
**The gate does not trust the plan's account of the world.** It re-reads every
fact it decides on from the store. The plan says the supplier is approved; the
gate looks the supplier up. A gate that validated the model's assertions against
the model's other assertions is an elaborate way of agreeing with it.

## Approval requirement and approval authority are different questions
Policy says no write happens without a human. Authority says whether this
person's limit covers this value. They are independent, and conflating them is
how an agent executes something nobody senior enough ever saw.

`approvals.py` holds the backup routing rule, which is time driven: it fires
when a request is unanswered at end of day **and** the approver is out the next
day, and it re-checks the backup's authority rather than assuming it transfers.
