"""Shared fixtures.

Every test gets a real harness against a real SQLite database in a temporary
directory. Nothing here mocks the store, the gate, the workflow engine or the
audit log, because those are the things under test. The single seam that is
scripted is the model client, and `stub_llm` implements the same interface the
real one does rather than patching over it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness.kernel import Harness  # noqa: E402
import stub_llm  # noqa: E402


@pytest.fixture
def make_harness(tmp_path):
    """Build a seeded harness with a scripted model client."""

    created: list[Harness] = []

    def _make(**script) -> Harness:
        index = len(created)
        harness = Harness(
            db_path=tmp_path / f"harmony-{index}.db",
            company_dir=ROOT / "company",
            runs_dir=tmp_path / f"runs-{index}",
            cassette_dir=tmp_path / "cassettes",
            llm=stub_llm.combined(**script),
        )
        harness.initialise()
        created.append(harness)
        return harness

    yield _make
    for harness in created:
        harness.close()


@pytest.fixture
def harness(make_harness):
    return make_harness()


@pytest.fixture
def reopen(tmp_path):
    """Reopen an existing database as a new process would.

    Used by the resumption tests: the point is that nothing lives in memory, so
    the test has to genuinely drop the old handle and build a new one.
    """

    def _reopen(index: int = 0, **script) -> Harness:
        return Harness(
            db_path=tmp_path / f"harmony-{index}.db",
            company_dir=ROOT / "company",
            runs_dir=tmp_path / f"runs-{index}",
            cassette_dir=tmp_path / "cassettes",
            llm=stub_llm.combined(**script),
        )

    return _reopen


def to_approval(harness: Harness, subject: str = "u-101"):
    """Detect, plan and gate, stopping at the approval boundary."""
    sweep = harness.detect()
    item = next(i for i in sweep.new_items if i.subject_user == subject)
    return harness.handle(item)


def approve_and_execute(harness: Harness, subject: str = "u-101", **kwargs):
    result = to_approval(harness, subject)
    harness.approvals.decide(
        result.approval["id"], verdict="approved",
        by=result.approval["requested_of"],
    )
    return harness.execute(result.approval["id"], **kwargs)
