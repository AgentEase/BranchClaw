#!/usr/bin/env python3
"""Publish a structured worker result back into BranchClaw."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from branchclaw.project_tools import emit_worker_report  # noqa: E402


def _read_json_or_file(value: str) -> dict:
    path = Path(value)
    raw = path.read_text(encoding="utf-8") if path.exists() and path.is_file() else value
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("Expected a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="", help="Override BRANCHCLAW_RUN_ID")
    parser.add_argument("--worker-name", default="", help="Override BRANCHCLAW_WORKER_NAME")
    parser.add_argument("--result-file", default="", help="Path to JSON payload")
    parser.add_argument("--status", default="", help="success|warning|blocked|failed")
    parser.add_argument("--stack", default="", help="Detected stack")
    parser.add_argument("--runtime", default="", help="Detected runtime")
    parser.add_argument("--package-manager", default="", help="Detected package manager")
    parser.add_argument("--install-command", default="", help="Install command used")
    parser.add_argument("--start-command", default="", help="Start command used")
    parser.add_argument("--preview-url", default="", help="Frontend preview URL")
    parser.add_argument("--backend-url", default="", help="Backend base URL")
    parser.add_argument("--output-snippet", default="", help="Relevant runtime output")
    parser.add_argument("--changed-surface-summary", default="", help="Changed surface summary")
    parser.add_argument("--architecture-summary", default="", help="Architecture markdown or path")
    parser.add_argument("--warning", action="append", default=[], help="Warning text")
    parser.add_argument("--blocker", action="append", default=[], help="Blocker text")
    args = parser.parse_args()

    payload = _read_json_or_file(args.result_file) if args.result_file else {}
    if args.architecture_summary:
        path = Path(args.architecture_summary)
        payload["architecture_summary"] = (
            path.read_text(encoding="utf-8") if path.exists() and path.is_file() else args.architecture_summary
        )
    payload.update(
        {
            key: value
            for key, value in {
                "status": args.status,
                "stack": args.stack,
                "runtime": args.runtime,
                "package_manager": args.package_manager,
                "install_command": args.install_command,
                "start_command": args.start_command,
                "preview_url": args.preview_url,
                "backend_url": args.backend_url,
                "output_snippet": args.output_snippet,
                "changed_surface_summary": args.changed_surface_summary,
            }.items()
            if value
        }
    )
    if args.warning:
        payload["warnings"] = args.warning
    if args.blocker:
        payload["blockers"] = args.blocker

    result = emit_worker_report(
        payload,
        run_id=args.run_id or None,
        worker_name=args.worker_name or None,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
