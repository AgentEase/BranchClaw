#!/usr/bin/env python3
"""Generate a Markdown architecture summary from a git diff."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from branchclaw.project_tools import generate_architecture_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--base-ref", default="HEAD", help="Base ref for diff")
    parser.add_argument("--head-ref", default="", help="Optional head ref")
    parser.add_argument("--out", default="", help="Optional output markdown file")
    args = parser.parse_args()

    summary = generate_architecture_summary(
        args.repo_root,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
    )
    if args.out:
        Path(args.out).write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
