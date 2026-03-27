#!/usr/bin/env python3
"""Wait for a local URL to appear in a service log."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from branchclaw.project_tools import discover_urls_from_text, wait_for_url  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-path", required=True, help="Service log file")
    parser.add_argument("--timeout-seconds", type=float, default=30.0, help="Wait budget")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    url = wait_for_url(args.log_path, timeout_seconds=args.timeout_seconds)
    log_text = Path(args.log_path).read_text(encoding="utf-8", errors="ignore") if Path(args.log_path).exists() else ""
    payload = {
        "url": url,
        "urls": discover_urls_from_text(log_text),
        "log_path": args.log_path,
    }
    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0 if url else 2


if __name__ == "__main__":
    raise SystemExit(main())
