#!/usr/bin/env python3
"""Manual BranchClaw long-run harness for a real Three.js / R3F repo."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from branchclaw.manual_threejs import (
    DEFAULT_DURATION_HOURS,
    DEFAULT_ITERATION_MINUTES,
    DEFAULT_REPO_URL,
    ThreejsLongrunError,
    ThreejsLongrunHarness,
    print_summary,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Target public repository URL.")
    parser.add_argument(
        "--artifact-dir",
        default="",
        help="Artifact directory (default: artifacts/manual-threejs/<timestamp>).",
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=DEFAULT_DURATION_HOURS,
        help="Overall long-run duration budget.",
    )
    parser.add_argument(
        "--iteration-minutes",
        type=int,
        default=DEFAULT_ITERATION_MINUTES,
        help="Budget per iteration before workers are stopped and archived.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Override iteration count. Defaults to 6 for 24h and 12 for 48h.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run a compressed one-iteration smoke (30 minutes total, 20 minute iteration budget).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    artifact_root = (
        Path(args.artifact_dir)
        if args.artifact_dir
        else Path.cwd() / "artifacts" / "manual-threejs" / timestamp
    )
    duration_hours = args.duration_hours
    iteration_minutes = args.iteration_minutes
    max_iterations = args.max_iterations or None
    if args.smoke:
        duration_hours = 0.5
        iteration_minutes = 20
        max_iterations = 1

    harness = ThreejsLongrunHarness(
        artifact_root=artifact_root,
        repo_url=args.repo_url,
        duration_hours=duration_hours,
        iteration_minutes=iteration_minutes,
        max_iterations=max_iterations,
    )
    try:
        exit_code = harness.run()
    except ThreejsLongrunError as exc:
        print_summary(artifact_root / "summary.md", stream=sys.stderr)
        print(f"[manual-threejs] {exc}", file=sys.stderr)
        return 1
    print_summary(artifact_root / "summary.md")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
