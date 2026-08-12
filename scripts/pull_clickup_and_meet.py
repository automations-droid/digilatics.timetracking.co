#!/usr/bin/env python3
"""
Pull ClickUp + Google Meet into Postgres (same jobs as the 30-min scheduler).

Usage:
  python scripts/pull_clickup_and_meet.py
  python scripts/pull_clickup_and_meet.py --clickup-only
  python scripts/pull_clickup_and_meet.py --meet-only

In Docker:
  docker compose exec time python scripts/pull_clickup_and_meet.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pull ClickUp + Google Meet into Postgres")
    p.add_argument("--clickup-only", action="store_true", help="Only run ClickUp sync")
    p.add_argument("--meet-only", action="store_true", help="Only run Google Meet sync")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.clickup_only and args.meet_only:
        print("Use only one of --clickup-only or --meet-only", file=sys.stderr)
        return 2

    run_clickup = not args.meet_only
    run_meet = not args.clickup_only
    results: dict[str, dict] = {}

    if run_clickup:
        print("Pulling ClickUp…")
        from ingest.clickup import run_sync

        results["clickup"] = run_sync()
        print(f"  ClickUp: +{results['clickup'].get('rowsInserted', 0)} rows")

    if run_meet:
        print("Pulling Google Meet…")
        from ingest.meet import run_sync

        results["meet"] = run_sync()
        print(f"  Meet:    +{results['meet'].get('rowsInserted', 0)} rows")

    print("\n=== Summary ===")
    print(json.dumps({k: v.get("rowsInserted", 0) for k, v in results.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
