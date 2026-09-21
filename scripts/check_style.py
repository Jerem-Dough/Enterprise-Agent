#!/usr/bin/env python3
"""Fail if a literal em dash or en dash reached the repository.

    python scripts/check_style.py          report and exit non-zero
    python scripts/check_style.py --fix    rewrite them as escape sequences

CLAUDE.md states the rule. This enforces it, because a rule nothing checks is
a preference. It runs in CI.

Checked by codepoint rather than by `grep -P '\\x{2014}'`, which is not
portable: BSD grep has no `-P`, and the pattern silently matches nothing on a
build where PCRE is absent, which is the worst kind of passing check.

Legitimate uses exist. `harness/workflows/po_reroute.py` needs the characters
to detect them, and a test needs one to prove the detection works. Both write
`\\u2014` as an escape sequence, which is the same character at runtime and no
character at all in the source. That keeps this check absolute, with no
allowlist to drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = {0x2014: "\\u2014", 0x2013: "\\u2013"}
EXTENSIONS = {".md", ".py", ".json", ".txt", ".yml", ".yaml"}
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv",
             "_demo", "_rec", "_record", "cassettes"}


def main() -> int:
    fix = "--fix" in sys.argv
    found = 0

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not any(chr(cp) in text for cp in BANNED):
            continue

        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            for cp in BANNED:
                if chr(cp) in line:
                    found += 1
                    print(f"{relative}:{number}: U+{cp:04X}")
        if fix:
            for cp, escape in BANNED.items():
                text = text.replace(chr(cp), escape)
            path.write_text(text, encoding="utf-8")
            print(f"  rewrote {relative}")

    if found and not fix:
        print(
            f"\n{found} literal occurrence(s). See CLAUDE.md. Read the clause and "
            f"pick the punctuation it needs: a full stop between whole thoughts, "
            f"a comma for an aside, a colon where the second half defines the "
            f"first. Do not bulk substitute."
        )
        return 1
    print(f"\nclean ({found} rewritten)" if fix else "\nclean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
