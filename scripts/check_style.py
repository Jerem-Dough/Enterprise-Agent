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

BANNED = {
    0x2014: "\\u2014",  # em dash, house style
    0x2013: "\\u2013",  # en dash, house style
    # Invisible characters, which are never intentional in source. A docstring
    # here once explained the BOM bug in .env parsing and contained a real BOM,
    # put there by an editor interpreting the escape. Nothing showed it.
    0xFEFF: "\\ufeff",  # byte order mark
    0x200B: "\\u200b",  # zero width space
    0x00A0: "\\u00a0",  # non-breaking space
}
EXTENSIONS = {".md", ".py", ".json", ".txt", ".yml", ".yaml"}
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv",
             "_demo", "_rec", "_record", "cassettes"}


NAMES = {
    0x2014: "em dash", 0x2013: "en dash", 0xFEFF: "byte order mark",
    0x200B: "zero width space", 0x00A0: "non-breaking space",
}


def main() -> int:
    fix = "--fix" in sys.argv
    found = 0
    kinds: set[str] = set()

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
                    kinds.add("dash" if cp in (0x2014, 0x2013) else "invisible")
                    print(f"{relative}:{number}: U+{cp:04X} ({NAMES[cp]})")
        if fix:
            for cp, escape in BANNED.items():
                text = text.replace(chr(cp), escape)
            path.write_text(text, encoding="utf-8")
            print(f"  rewrote {relative}")

    if found and not fix:
        print(f"\n{found} occurrence(s).")
        if "dash" in kinds:
            print(
                "  Dashes: see CLAUDE.md. Read the clause and pick the "
                "punctuation it needs, a full stop between whole thoughts, a "
                "comma for an aside, a colon where the second half defines the "
                "first. Do not bulk substitute."
            )
        if "invisible" in kinds:
            print(
                "  Invisible characters are never intentional in source. If one "
                "is genuinely needed, write it as an escape sequence."
            )
        return 1
    print(f"\nclean ({found} rewritten)" if fix else "\nclean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
