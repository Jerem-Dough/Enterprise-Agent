# tests/: proving the guarantees, not the functions

One job: assert that the things the brief calls for are actually enforced.

## Inputs
- Reference (every run): `../company/`, and a temporary database per test
- Reference (every run): `stub_llm.py`, the scripted model client

## Process
Each test builds a real harness against a real SQLite database. Nothing mocks
the store, the gate, the workflow engine or the audit log, because those are the
things under test. The one scripted seam is the model client, and it implements
the same interface the real one does rather than patching over it.

## Outputs
- `python -m pytest`, 69 tests

## Human check
Read an assertion. An authorization test should check that the thing was
*refused* and that the world is unchanged afterwards. A gate test that only
checks a boolean is checking that a function returns, not that a purchase order
failed to exist.

## What is covered

| File | What it proves |
|---|---|
| `test_gate.py` | Permissions, policy, escalation, and that a refusal writes nothing |
| `test_dedupe.py` | A repeated sweep is silent, a genuinely new situation is not |
| `test_workflow.py` | Declared order, bounded model steps, resumption, compensation |
| `test_approvals.py` | Both halves of the backup rule, firing and not firing |
| `test_audit.py` | Append only, tamper evident, and sufficient on its own |

`test_audit.py::test_the_log_alone_answers_all_five_questions` is the one that
matters most. It holds only the log and answers what the agent saw, what it
concluded, what it was allowed to do, who approved, and what happened.
