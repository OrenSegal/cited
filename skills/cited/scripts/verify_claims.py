#!/usr/bin/env python3
"""cited: check that each claim is on the page it cites.

Reads a JSON array of {"id", "claim", "source_url"} objects (a file, or - for
stdin), fetches every source_url, and reports a tier per claim. It checks
containment, not truth: a verified claim is on the page, whether or not the
page is right."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fetcher import (
    DEFAULT_PER_HOST_DELAY,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    MAX_RETRY_WAIT,
    Fetcher,
    bot_walled_host,
    canonicalize_for_fetch,
    is_challenge_page,
    log,
    utc_now,
)
from page_text import UNREADABLE_KINDS
from safe_fetch import VERSION, FetchPolicy, FetchResult
from tiering_core import (
    MIN_PAGE_TEXT_CHARS,
    TIER_BROKEN,
    TIER_LABELS,
    TIER_LOW_MATCH,
    TIER_SNIPPET_ONLY,
    TIER_UNSUPPORTED,
    TIER_UNVERIFIED,
    TIERS,
    blocking_reason,
    is_blocking,
    tier_for_claim,
)

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_BLOCKING = 1
EXIT_USAGE = 2
EXIT_OFFLINE_MISS = 3
EXIT_INTERNAL_ERROR = 4
EXIT_INTERRUPTED = 130

EXIT_CODES = {
    EXIT_OK: "Nothing blocking.",
    EXIT_BLOCKING: "At least one blocking claim.",
    EXIT_USAGE: "Usage error or invalid claims file. Nothing was fetched.",
    EXIT_OFFLINE_MISS: "--offline and at least one source had no cached copy, and no other code applies.",
    EXIT_INTERNAL_ERROR: "An internal error while checking a claim, or --annotate-out could not be written, "
                         "and nothing was blocking. The other claims are still checked and reported.",
    EXIT_INTERRUPTED: "Interrupted.",
}

DEFAULT_CONCURRENCY = 4
MAX_LISTED_PROBLEMS = 50


@dataclass
class Verdict:
    tier: str
    note: str
    quoted: float = 0.0
    topical: float = 0.0
    checked_against: str = "none"   # "live" | "wayback" | "none"
    http_status: int | None = None
    fetched_url: str | None = None
    snapshot_url: str | None = None
    offline_miss: bool = False
    internal_error: bool = False


def _verdict_from_text(claim: str, page: FetchResult, max_bytes: int) -> tuple[str, str, float, float]:
    tier, note, quoted, topical = tier_for_claim(claim, page.text)
    if page.truncated and tier == TIER_UNSUPPORTED:
        tier = TIER_UNVERIFIED
        note = (f"Page is larger than the {max_bytes:,}-byte read limit and the claim was not in the part "
                f"that was read, so its absence is not proven ({note})")
    return tier, note, quoted, topical


def check_source(url: str, claim: str, fetcher: Fetcher) -> Verdict:
    live = fetcher.page(canonicalize_for_fetch(url))
    if live.blocked:
        # Never forwarded to the Wayback Machine: that would hand an internal
        # hostname to a third party.
        return Verdict(TIER_BROKEN, f"Source URL {live.error}")
    if live.extra.get("offline_miss"):
        return Verdict(TIER_UNVERIFIED, "Offline: no cached copy of this source. Run once online with "
                       "--cache to record it.", offline_miss=True)

    status = live.status
    live_failed = status is None or status >= 400
    unreadable = not live_failed and live.kind in UNREADABLE_KINDS
    too_thin = not live_failed and not unreadable and len(live.text.strip()) < MIN_PAGE_TEXT_CHARS
    base = {"http_status": status, "fetched_url": live.final_url or None}

    if (live_failed or too_thin) and fetcher.use_wayback:
        snapshot_url, snapshot_date, archived = fetcher.wayback(url)
        archived_ok = archived is not None and archived.status is not None and archived.status < 400
        if archived_ok and archived.text:
            tier, note, quoted, topical = _verdict_from_text(claim, archived, fetcher.policy.max_bytes)
            why = "unreachable" if live_failed else "yielded no text"
            suffix = f"checked against Wayback archive ({snapshot_date or 'undated'}), live page {why}"
            return Verdict(tier, f"{note} — {suffix}" if note else suffix, quoted, topical,
                           checked_against="wayback", snapshot_url=snapshot_url, **base)
        if live_failed and archived_ok and archived.kind in UNREADABLE_KINDS:
            what = "a PDF" if archived.kind == "pdf" else "non-text content"
            return Verdict(TIER_UNVERIFIED, f"Live page unreachable; the Wayback copy ({snapshot_date or 'undated'}) "
                           f"is {what}, which cited does not read. Check this claim against it by hand.",
                           snapshot_url=snapshot_url, **base)

    if status == 429:
        return Verdict(TIER_SNIPPET_ONLY, "Source rate-limited the fetch (HTTP 429) and no archived copy "
                       "found; claim is snippet-sourced only", **base)
    domain = bot_walled_host(url)
    if (status in (403, 429) or is_challenge_page(status, live.text)) and domain:
        return Verdict(TIER_SNIPPET_ONLY, f"{domain} blocks automated fetch and no archived copy found; "
                       "claim is snippet-sourced only", **base)
    if live_failed:
        detail = f"HTTP {status}" if status is not None else (live.error or "no response")
        archive = "no archived copy found" if fetcher.use_wayback else "Wayback fallback disabled"
        return Verdict(TIER_BROKEN, f"Source unreachable ({detail}) and {archive}", **base)
    if unreadable:
        what = "a PDF" if live.kind == "pdf" else f"non-text content ({live.content_type or 'unknown type'})"
        return Verdict(TIER_UNVERIFIED, f"Source is {what}; cited reads HTML and plain-text pages only. "
                       "Check this claim against the document by hand.", checked_against="none", **base)
    tier, note, quoted, topical = _verdict_from_text(claim, live, fetcher.policy.max_bytes)
    return Verdict(tier, note, quoted, topical, checked_against="live", **base)


class InputError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def load_claims(source: str) -> list[dict[str, Any]]:
    """Read and validate the claims array. Raises InputError listing every
    problem found (up to MAX_LISTED_PROBLEMS), so a bad file is fixed in one pass."""
    label = "stdin" if source == "-" else source
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise InputError([f"{label}: file not found"]) from None
    except UnicodeDecodeError as exc:
        raise InputError([f"{label}: not valid UTF-8 ({exc.reason} at byte {exc.start})"]) from None
    except OSError as exc:
        raise InputError([f"{label}: cannot read ({exc.strerror or exc})"]) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError([f"{label}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]) from None
    if not isinstance(data, list):
        raise InputError([f"{label}: top level must be a JSON array of {{id, claim, source_url}} objects, "
                          f"got a JSON {_json_type(data)}"])

    problems: list[str] = []
    for index, entry in enumerate(data):
        where = f"{label}[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: expected an object, got a JSON {_json_type(entry)}")
            continue
        for key in ("claim", "source_url"):
            if key not in entry:
                problems.append(f"{where}: missing required field '{key}'")
            elif not isinstance(entry[key], str):
                problems.append(f"{where}: '{key}' must be a string, got a JSON {_json_type(entry[key])}")
        if isinstance(entry.get("claim"), str) and not entry["claim"].strip():
            problems.append(f"{where}: 'claim' is empty")
        if "id" in entry and (isinstance(entry["id"], bool) or not isinstance(entry["id"], (str, int))):
            problems.append(f"{where}: 'id' must be a string or integer, got a JSON {_json_type(entry['id'])}")
    if problems:
        extra = len(problems) - MAX_LISTED_PROBLEMS
        raise InputError(problems[:MAX_LISTED_PROBLEMS] + ([f"... and {extra} more"] if extra > 0 else []))

    seen: dict[str, int] = {}
    for index, entry in enumerate(data):
        if "id" in entry:
            key = str(entry["id"])
            if key in seen:
                log(f"warning: id {key!r} appears at [{seen[key]}] and [{index}]")
            seen.setdefault(key, index)
    return data


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return {dict: "object", list: "array", str: "string", int: "number", float: "number"}.get(type(value), "value")


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _int_range(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    epilog = "exit codes:\n" + "\n".join(f"  {code:<4} {meaning}" for code, meaning in EXIT_CODES.items())
    parser = argparse.ArgumentParser(
        prog="cited", description=__doc__, epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="JSON array of {id, claim, source_url}; - reads stdin")
    parser.add_argument("--version", action="version", version=f"cited {VERSION}")
    out = parser.add_argument_group("output")
    out.add_argument("--json", action="store_true",
                     help="print a machine-readable report (schema in README) to stdout instead of the table")
    out.add_argument("--annotate-out", type=Path, metavar="PATH",
                     help="write the input back out with verification_tier/verification_note/verified_at on every entry")
    out.add_argument("--strict", action="store_true",
                     help=f"also fail (exit {EXIT_BLOCKING}) on unverified and snippet_only: claims that were never checked")
    net = parser.add_argument_group("fetching")
    net.add_argument("--timeout", type=_positive_float, default=DEFAULT_TIMEOUT, metavar="SEC",
                     help="per-request time limit, connect through last byte (default %(default)s)")
    net.add_argument("--retries", type=_int_range(0, 5), default=DEFAULT_RETRIES,
                     help="retries for timeouts, connection errors, 429 and 5xx, with exponential backoff; "
                          f"Retry-After is honored up to {MAX_RETRY_WAIT:g}s (default %(default)s)")
    net.add_argument("--concurrency", type=_int_range(1, 32), default=DEFAULT_CONCURRENCY,
                     help="claims checked in parallel (default %(default)s)")
    net.add_argument("--per-host-delay", type=_non_negative_float, default=DEFAULT_PER_HOST_DELAY, metavar="SEC",
                     help="minimum gap between requests to the same host (default %(default)s)")
    net.add_argument("--no-wayback", action="store_true",
                     help="do not fall back to the Wayback Machine for dead or empty pages")
    net.add_argument("--max-bytes", type=_int_range(10_000, 100_000_000), default=FetchPolicy.max_bytes,
                     help="read at most this many bytes per page (default %(default)s)")
    net.add_argument("--max-redirects", type=_int_range(0, 20), default=FetchPolicy.max_redirects,
                     help="follow at most this many redirects (default %(default)s)")
    cache = parser.add_argument_group("reproducible runs")
    cache.add_argument("--cache", type=Path, metavar="DIR",
                       help="read fetched pages from DIR when present, and store new ones there")
    cache.add_argument("--offline", action="store_true",
                       help=f"never touch the network; use only --cache (missing entries exit {EXIT_OFFLINE_MISS})")
    sec = parser.add_argument_group("address policy (see SECURITY.md before using)")
    sec.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                     help="let HOST resolve to a private address (intranet sources); repeatable")
    sec.add_argument("--allow-private-addresses", action="store_true",
                     help="disable the private/loopback address block entirely (tests, trusted networks)")
    sec.add_argument("--proxy-from-env", action="store_true",
                     help="honor HTTP(S)_PROXY; the address check then runs before the proxy, not at connect")
    return parser


def _check_entry(index: int, entry: dict[str, Any], fetcher: Fetcher) -> Verdict:
    url = entry["source_url"].strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return Verdict(TIER_BROKEN, "Invalid source_url: must be an absolute http(s) URL")
    try:
        return check_source(url, entry["claim"], fetcher)
    except Exception as exc:  # noqa: BLE001 — one bad claim must not sink the run
        log(f"internal error checking claim [{index}]: {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        return Verdict(TIER_UNVERIFIED, f"Internal error while checking: {type(exc).__name__}: {exc}",
                       internal_error=True)


def _exit_code(reason: str | None, offline_misses: int, internal_errors: int) -> int:
    if reason:
        return EXIT_BLOCKING
    if internal_errors:
        return EXIT_INTERNAL_ERROR
    if offline_misses:
        return EXIT_OFFLINE_MISS
    return EXIT_OK


def _print_table(rows: list[dict[str, Any]], counts: dict[str, int], offline_misses: int, strict: bool) -> None:
    print(f"{'ID':<26} {'TIER':<14} {'QUOTED':>6} {'TOPICAL':>7}  URL")
    for row in rows:
        print(f"{row['id'][:26]:<26} {row['label']:<14} {row['quoted']:>6.2f} {row['topical']:>7.2f}  {row['source_url']}")
    summary = ", ".join(f"{counts[t]} {TIER_LABELS[t].lower()}" for t in TIERS if counts.get(t)) or "none"
    print(f"\n{len(rows)} claims checked — {summary}.")
    if counts.get(TIER_UNSUPPORTED):
        print(f"\n{counts[TIER_UNSUPPORTED]} claim(s) are NOT ON THE PAGE they cite. The source loaded and was "
              "readable, and the claim isn't in it — treat as fabricated until proven otherwise.")
    if counts.get(TIER_BROKEN):
        print(f"\n{counts[TIER_BROKEN]} source(s) are unreachable or invalid. Drop the claim or find a working source.")
    if counts.get(TIER_LOW_MATCH):
        print(f"\n{counts[TIER_LOW_MATCH]} claim(s) only share vocabulary with their page and are not quoted from it. "
              "A human must check each one against the page, or tighten it to what the page says, before shipping.")
    unchecked = counts.get(TIER_UNVERIFIED, 0) + counts.get(TIER_SNIPPET_ONLY, 0)
    if strict and unchecked:
        print(f"\n--strict: {unchecked} claim(s) were never checked against page text (unverified or snippet-only).")
    if offline_misses:
        print(f"\n--offline: {offline_misses} source(s) had no cached copy. Re-run online with --cache to record them.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.offline and not args.cache:
        parser.error("--offline needs --cache DIR to read from")

    try:
        entries = load_claims(args.input)
    except InputError as exc:
        print("cited: invalid input:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_USAGE

    policy = FetchPolicy(
        allow_private=args.allow_private_addresses,
        allow_hosts=frozenset(h.strip().rstrip(".").lower() for h in args.allow_host if h.strip()),
        max_bytes=args.max_bytes,
        max_redirects=args.max_redirects,
        use_env_proxy=args.proxy_from_env,
    )
    fetcher = Fetcher(policy=policy, timeout=args.timeout, retries=args.retries,
                      per_host_delay=args.per_host_delay, cache_dir=args.cache,
                      offline=args.offline, use_wayback=not args.no_wayback)

    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    try:
        futures = [pool.submit(_check_entry, i, entry, fetcher) for i, entry in enumerate(entries)]
        verdicts = [future.result() for future in futures]
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        print("\ncited: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    pool.shutdown()

    checked_on = datetime.now(timezone.utc).date().isoformat()
    counts = {tier: 0 for tier in TIERS}
    rows: list[dict[str, Any]] = []
    for index, (entry, verdict) in enumerate(zip(entries, verdicts, strict=True)):
        counts[verdict.tier] = counts.get(verdict.tier, 0) + 1
        entry["verification_tier"] = verdict.tier
        entry["verification_note"] = verdict.note
        entry["verified_at"] = checked_on
        rows.append({
            "index": index,
            "id": str(entry["id"]) if "id" in entry else f"#{index}",
            "claim": entry["claim"],
            "source_url": entry["source_url"],
            "tier": verdict.tier,
            "label": TIER_LABELS[verdict.tier],
            "blocking": is_blocking(verdict.tier, args.strict),
            "note": verdict.note,
            "quoted": round(verdict.quoted, 4),
            "topical": round(verdict.topical, 4),
            "checked_against": verdict.checked_against,
            "http_status": verdict.http_status,
            "fetched_url": verdict.fetched_url,
            "snapshot_url": verdict.snapshot_url,
        })

    offline_misses = sum(v.offline_miss for v in verdicts)
    internal_errors = sum(v.internal_error for v in verdicts)
    reason = blocking_reason(counts, strict=args.strict)
    code = _exit_code(reason, offline_misses, internal_errors)

    annotated = False
    if args.annotate_out:
        try:
            args.annotate_out.parent.mkdir(parents=True, exist_ok=True)
            args.annotate_out.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            annotated = True
        except OSError as exc:
            log(f"could not write {args.annotate_out}: {exc.strerror or exc}")
            code = EXIT_BLOCKING if reason else EXIT_INTERNAL_ERROR

    if args.json:
        report = {
            "schema_version": SCHEMA_VERSION,
            "cited_version": VERSION,
            "checked_at": utc_now(),
            "options": {"strict": args.strict, "offline": args.offline, "wayback": not args.no_wayback},
            "summary": {
                "total": len(rows),
                "counts": counts,
                "blocking": reason,
                "offline_misses": offline_misses,
                "internal_errors": internal_errors,
                "exit_code": code,
            },
            "results": rows,
        }
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _print_table(rows, counts, offline_misses, args.strict)
        if annotated:
            print(f"\nAnnotated JSON written: {args.annotate_out.resolve()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
