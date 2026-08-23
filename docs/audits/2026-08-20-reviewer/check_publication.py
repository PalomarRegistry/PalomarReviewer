#!/usr/bin/env python3
"""Heuristic privacy check for the allowlisted public reviewer-audit bundle.

This does not replace human review. It catches common accidental linkages:
submission IDs, full source commits, registry IDs, unexpected files, and links
from the public bundle into the restricted evidence archive.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "PUBLICATION_MANIFEST.txt"

PATTERNS = {
    "possible submission id": re.compile(
        r"(?<![A-Za-z0-9])(?=[a-z0-9]{12}(?![A-Za-z0-9]))(?=[a-z0-9]*\d)[a-z0-9]{12}"
    ),
    "full 40-hex source or state commit": re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])"),
    "registry identifier": re.compile(r"PALOMAR-\d{4}-\d{2}-\d{2}-\d{6}"),
    "absolute local path": re.compile(
        r"(?:/" + r"home/|/" + r"Users/|[A-Za-z]:\\\\)"
    ),
    "parent-directory markdown link": re.compile(r"\]\((?:\.\./|/)[^)]+\)"),
}


def load_manifest() -> set[str]:
    return {
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--denylist",
        type=Path,
        help="restricted newline-delimited identifiers to reject without echoing",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    allowed = load_manifest()
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.iterdir()
    }
    problems: list[str] = []

    unexpected = sorted(actual - allowed)
    missing = sorted(allowed - actual)
    if unexpected:
        problems.append(f"unexpected files: {', '.join(unexpected)}")
    if missing:
        problems.append(f"manifest files missing: {', '.join(missing)}")

    for relative in sorted(allowed):
        path = ROOT / relative
        if not path.exists() or path.suffix not in {".md", ".txt", ".py", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{relative}:{line}: {label}")

        if args.denylist:
            folded = text.casefold()
            for token in args.denylist.read_text(encoding="utf-8").splitlines():
                token = token.strip()
                if len(token) < 6 or token.casefold() not in folded:
                    continue
                problems.append(f"{relative}: contains a restricted denylist value")

    if problems:
        print("PUBLICATION CHECK FAILED", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print(f"Publication check passed for {len(allowed)} allowlisted entries.")
    print("Human privacy review is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
