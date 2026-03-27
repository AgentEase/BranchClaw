#!/usr/bin/env python3
"""Detect the project stack for BranchClaw project-profile skills."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from branchclaw.project_tools import detect_project_stack  # noqa: E402


def _suggested_profile(info: dict[str, object]) -> str:
    if info.get("frontend") and info.get("backend"):
        return "fullstack"
    if info.get("frontend"):
        return "web"
    return "backend"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    info = detect_project_stack(args.repo_root)
    info["supported"] = info.get("runtime") in {"node", "python"}
    info["suggested_profile"] = _suggested_profile(info)
    text = json.dumps(info, indent=2 if args.pretty else None, ensure_ascii=False)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
