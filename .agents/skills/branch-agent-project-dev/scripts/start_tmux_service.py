#!/usr/bin/env python3
"""Start a detached tmux service and print structured metadata."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from branchclaw.project_tools import launch_tmux_service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--session", "--session-name", dest="session", required=True, help="tmux session name")
    parser.add_argument("--window", "--window-name", dest="window", required=True, help="tmux window name")
    parser.add_argument("--log-path", required=True, help="Path to append service logs")
    parser.add_argument(
        "--command",
        dest="command_text",
        default="",
        help="Command to run as a shell string, for example 'npm run dev -- --host 127.0.0.1 --port 4173'",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment entry in KEY=VALUE form; repeatable",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after `--`, for example -- npm run dev",
    )
    args = parser.parse_args()

    command = shlex.split(args.command_text) if args.command_text else list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("Missing service command after --")

    env = {}
    for item in args.env:
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"Invalid --env value: {item}")
        env[key] = value

    result = launch_tmux_service(
        session_name=args.session,
        window_name=args.window,
        cwd=args.repo_root,
        command=command,
        log_path=args.log_path,
        env=env,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
