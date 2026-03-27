#!/usr/bin/env python3
"""Install project dependencies for project-profile workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from branchclaw.project_tools import detect_project_stack, install_dependencies  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    info = detect_project_stack(args.repo_root)
    result = install_dependencies(args.repo_root, info)
    payload = {
        "project": info,
        "install": {
            **result,
            "command": " ".join(result.get("command", [])),
        },
    }
    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
