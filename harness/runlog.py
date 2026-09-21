"""The glass box: one folder per run, readable without running anything.

The store is the state machine; the filesystem is the explanation surface.
Every run leaves numbered plain files: what the detector noticed, what each
provider returned, the exact context window, the plan, each gate rule, the
approval, each step, and the mirrored audit.

Nothing here is authoritative. Delete `runs/` and the harness knows everything
it knew; what is lost is the ability to understand a run by opening a folder,
which turns out to be most of what anyone wants during an incident.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RunFolder:
    """The artifact directory for one run."""

    def __init__(self, runs_dir: str | Path, run_id: str) -> None:
        self.path = Path(runs_dir) / run_id
        self.path.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, payload: Any) -> Path:
        """Write one numbered artifact. Overwrites, because a re-run of the
        same phase in the same run supersedes rather than accumulates. The
        audit log keeps both, which is where accumulation belongs."""
        target = self.path / name
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n",
            encoding="utf-8",
        )
        return target

    def write_step(self, index: int, step_id: str, payload: Any) -> Path:
        steps = self.path / "07-steps"
        steps.mkdir(exist_ok=True)
        target = steps / f"{index:02d}-{step_id}.json"
        target.write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return target

    def append_audit(self, record: dict) -> None:
        with (self.path / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def write_text(self, name: str, text: str) -> Path:
        target = self.path / name
        target.write_text(text, encoding="utf-8")
        return target

    def read(self, name: str) -> Any:
        return json.loads((self.path / name).read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return (self.path / name).exists()
