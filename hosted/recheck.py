#!/usr/bin/env python3
"""Re-check a stored certificate run and report which verdicts changed.

The $9 certificate promises a 30-day re-check. This is that re-check, as a
script the owner runs (by hand or from cron); nothing schedules it yet.

  python3 hosted/recheck.py run.json            a run JSON on disk
  python3 hosted/recheck.py --id AbC123...      certs/{id}.json from the app's storage
                                                (Supabase or local, chosen like the app does)
  --json                                        machine-readable diff on stdout
  --save                                        with --id, store the new run as certs/{id}.recheck.json

Exit codes: 0 nothing changed, 1 at least one verdict changed, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "cited" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetcher import Fetcher, utc_now  # noqa: E402
from safe_fetch import VERSION, FetchPolicy  # noqa: E402
from storage import StorageError, storage_from_env  # noqa: E402
from tiering_core import TIER_LABELS, plural  # noqa: E402
from verify_claims import check_entries  # noqa: E402

EXIT_SAME, EXIT_CHANGED, EXIT_USAGE = 0, 1, 2


def entries_from_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The claims a stored run checked, rebuilt from its results."""
    return [{"id": row["id"], "claim": row["claim"], "source_url": row["source_url"],
             "line": row.get("line"), "bucket": row.get("bucket", "checkable")}
            for row in sorted(run["results"], key=lambda r: r.get("index", 0))]


REQUIRED_RESULT_FIELDS = ("id", "claim", "source_url", "tier")


def _validate_run(run: Any) -> None:
    """Raise ValueError unless every stored result has the fields a re-check needs."""
    results = run.get("results") if isinstance(run, dict) else None
    if not isinstance(results, list):
        raise ValueError("the run has no results list")
    for i, row in enumerate(results):
        missing = [f for f in REQUIRED_RESULT_FIELDS if not isinstance(row, dict) or f not in row]
        if missing:
            raise ValueError(f"result {i} is missing {', '.join(missing)}")


def recheck(run: dict[str, Any], fetcher: Fetcher, concurrency: int = 8) -> dict[str, Any]:
    """Re-run every claim in `run`; return {checked_at, counts, results, changed}."""
    rows, counts, _ = check_entries(entries_from_run(run), fetcher, concurrency=concurrency, draft=True)
    # Pair by position, not id: ids can repeat, and rows come back in entry order.
    before = sorted(run["results"], key=lambda r: r.get("index", 0))
    changed = [{"id": row["id"], "claim": row["claim"], "source_url": row["source_url"],
                "was": old["tier"], "now": row["tier"], "note": row["note"]}
               for old, row in zip(before, rows, strict=True) if old["tier"] != row["tier"]]
    return {"cited_version": VERSION, "checked_at": utc_now(), "previous_checked_at": run.get("checked_at"),
            "document": run.get("document"), "counts": counts, "results": rows, "changed": changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recheck", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("run", nargs="?", type=Path, help="a stored run JSON (certs/{id}.json)")
    source.add_argument("--id", help="certificate id to load from the app's storage")
    parser.add_argument("--json", action="store_true", help="print the diff as JSON")
    parser.add_argument("--save", action="store_true", help="with --id, store the new run next to the old one")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--no-wayback", action="store_true")
    args = parser.parse_args(argv)
    if args.save and not args.id:
        parser.error("--save needs --id")

    storage = None
    try:
        if args.id:
            storage = storage_from_env()
            raw = storage.get(f"certs/{args.id}.json")
            if raw is None:
                print(f"recheck: no stored run for {args.id}", file=sys.stderr)
                return EXIT_USAGE
        else:
            raw = args.run.read_bytes()
        run = json.loads(raw)
        _validate_run(run)
    except (OSError, StorageError, ValueError, KeyError, TypeError) as exc:
        print(f"recheck: cannot read the run: {exc}", file=sys.stderr)
        return EXIT_USAGE

    fetcher = Fetcher(policy=FetchPolicy(), timeout=args.timeout, retries=1, use_wayback=not args.no_wayback)
    report = recheck(run, fetcher)
    if args.save and storage is not None:
        storage.put(f"certs/{args.id}.recheck.json", json.dumps(report, ensure_ascii=False).encode("utf-8"),
                    "application/json")

    if args.json:
        json.dump({k: v for k, v in report.items() if k != "results"}, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(f"{report['document']}: {plural(len(report['results']), 'claim')} re-checked, first checked "
              f"{report['previous_checked_at']}, now {report['checked_at']}.")
        if not report["changed"]:
            print("No verdict changed.")
        for item in report["changed"]:
            print(f"\n{item['id']}: {TIER_LABELS.get(item['was'], item['was'])} -> "
                  f"{TIER_LABELS.get(item['now'], item['now'])}\n  {item['claim'][:160]}\n  {item['source_url']}\n"
                  f"  {item['note']}")
    return EXIT_CHANGED if report["changed"] else EXIT_SAME


if __name__ == "__main__":
    sys.exit(main())
