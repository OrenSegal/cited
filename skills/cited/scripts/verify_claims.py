#!/usr/bin/env python3
"""Verify claims against their cited sources before shipping an artifact.

For each claim in a JSON input, checks that source_url resolves and that the
cited claim text actually appears on the fetched page. This catches dead
links, wrong URLs, claims paraphrased from a search snippet rather than the
page itself, and — the case that matters most — claims that are simply not
in the source at all.

It is a containment check, not proof of authenticity: it can tell you a claim
is absent from a page, and it can tell you a claim is quoted from one. It
cannot tell you the page is honest, or that a correctly-quoted line means
what your artifact says it means.

Input: a JSON array (a file path, or - for stdin) of objects:
    {"id": "<optional stable identifier>", "claim": "<text that should be on
     the page>", "source_url": "<https://...>"}

Usage:
    cited claims.json [--json] [--annotate-out OUT.json] [--cache DIR [--offline]]
    python3 verify_claims.py claims.json ...   (same thing, without the plugin's bin/)

Exit codes:
    0  nothing blocking
    1  at least one blocking result: unsupported, broken or low_match
       (with --strict, also unverified and snippet_only)
    2  usage error or invalid input; nothing was fetched
    3  --offline and at least one source had no cached copy (only if not 1)
    4  internal error while checking a claim, or --annotate-out could not be
       written (only if not 1)
    130 interrupted
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from safe_fetch import (
    VERSION,
    FetchPolicy,
    FetchResult,
    _TextExtractor,  # noqa: F401 — re-exported; tests and older callers import it from here
    fetch_page,
    url_problem,
)
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

BOT_WALLED_DOMAINS = (
    "reddit.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "glassdoor.com",
    "indeed.com",
)


def _log(message: str) -> None:
    print(f"[cited] {message}", file=sys.stderr)


def bot_walled_host(url: str) -> str | None:
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    for domain in BOT_WALLED_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


CHALLENGE_MARKERS = ("please wait for verification", "checking your browser")


def canonicalize_for_fetch(url: str) -> str:
    """reddit.com/www.reddit.com serve a client-side bot-verification stub to
    script fetches; old.reddit.com serves the same page server-rendered with
    no such wall. Rewrite just the fetch target — the URL shown to the user
    is untouched."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host in ("reddit.com", "www.reddit.com"):
        netloc = "old.reddit.com" + (f":{parsed.port}" if parsed.port else "")
        return parsed._replace(netloc=netloc).geturl()
    return url


def is_challenge_page(status: int | None, text: str) -> bool:
    if status != 200 or len(text) > 200:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def fetch_text(url: str, timeout: float, policy: FetchPolicy | None = None) -> tuple[int | None, str]:
    """(HTTP status or None, extracted text) for one guarded fetch. Kept as
    the simple interface; check_source uses Fetcher for retries and caching."""
    result = fetch_page(canonicalize_for_fetch(url), timeout, policy)
    if result.error and result.status is None:
        _log(f"fetch failed for {url}: {result.error}")
    return result.status, result.text


# ── Fetcher: retries, per-host politeness, de-duplication, disk cache ──────

WAYBACK_API = "https://archive.org/wayback/available?url="
CACHE_FORMAT = 1


class HostThrottle:
    """Spaces requests to the same host at least `delay` seconds apart,
    across threads. Each caller reserves the next free slot, so waiting
    callers never hold the lock."""

    def __init__(self, delay: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.delay = delay
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(host, now))
            self._next[host] = slot + self.delay
        if slot > now:
            self._sleep(slot - now)


class Fetcher:
    def __init__(self, *, policy: FetchPolicy | None = None, timeout: float = 10.0,
                 retries: int = 2, backoff: float = 1.0, max_retry_wait: float = 30.0,
                 per_host_delay: float = 1.0, cache_dir: Path | None = None,
                 offline: bool = False, use_wayback: bool = True,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.policy = policy or FetchPolicy()
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_retry_wait = max_retry_wait
        self.cache_dir = cache_dir
        self.offline = offline
        self.use_wayback = use_wayback
        self.wayback_api = WAYBACK_API
        self._sleep = sleep
        self._throttle = HostThrottle(per_host_delay, sleep=sleep)
        self._memo: dict[tuple[str, str], Future] = {}
        self._lock = threading.Lock()

    # Every distinct URL is fetched at most once per run, even when several
    # claims cite it and are checked concurrently.
    def _once(self, key: tuple[str, str], compute: Callable[[], Any]) -> Any:
        with self._lock:
            future = self._memo.get(key)
            owner = future is None
            if owner:
                future = self._memo[key] = Future()
        if owner:
            try:
                future.set_result(compute())
            except BaseException as exc:
                future.set_exception(exc)
        return future.result()

    def page(self, url: str) -> FetchResult:
        return self._once(("page", url), lambda: self._page(url))

    def _page(self, url: str) -> FetchResult:
        problem = url_problem(url, self.policy)
        if problem:
            return FetchResult(error=f"refused: {problem}", blocked=True)
        cached = self._cache_get("page", url)
        if cached is not None:
            return FetchResult(**{k: v for k, v in cached.items() if k in _RESULT_FIELDS})
        if self.offline:
            return FetchResult(error="offline: no cached copy", extra={"offline_miss": True})
        result = self._fetch_with_retries(url)
        if result.error and result.status is None:
            _log(f"fetch failed for {url}: {result.error}")
        if not result.blocked and not result.transient:
            self._cache_put("page", url, dataclasses.asdict(result))
        return result

    def _fetch_with_retries(self, url: str) -> FetchResult:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        attempt = 0
        while True:
            self._throttle.wait(host)
            result = fetch_page(url, self.timeout, self.policy)
            walled = bot_walled_host(url) and result.status in (403, 429)
            if not result.transient or walled or attempt >= self.retries:
                return result
            if result.retry_after is not None:
                delay = result.retry_after
            else:
                delay = self.backoff * (2 ** attempt) + random.uniform(0, self.backoff / 4)
            if delay > self.max_retry_wait:
                return result  # the server asked for longer than we are willing to wait
            self._sleep(delay)
            attempt += 1

    def wayback(self, url: str) -> tuple[str, str, FetchResult | None]:
        """(snapshot_url, YYYYMMDD, fetched snapshot) for the closest Wayback
        Machine capture of `url`, or ("", "", None)."""
        return self._once(("wayback", url), lambda: self._wayback(url))

    def _wayback(self, url: str) -> tuple[str, str, FetchResult | None]:
        lookup = self._cache_get("wayback", url)
        if lookup is None:
            if self.offline:
                return "", "", None
            api = self.wayback_api + urllib.parse.quote(url, safe="")
            response = self._fetch_with_retries(api)
            if response.status != 200 or not response.text:
                if response.error:
                    _log(f"wayback lookup failed for {url}: {response.error}")
                return "", "", None
            try:
                info = json.loads(response.text)
                closest = (info.get("archived_snapshots") or {}).get("closest") or {}
            except (ValueError, AttributeError) as exc:
                _log(f"wayback lookup for {url} returned unreadable JSON: {exc}")
                return "", "", None
            snapshot_url = closest.get("url") if closest.get("available") else ""
            capture_status = str(closest.get("status") or "")
            if capture_status and not capture_status.startswith(("2", "3")):
                snapshot_url = ""  # the archive captured an error page; it proves nothing
            lookup = {
                "snapshot_url": snapshot_url if isinstance(snapshot_url, str) else "",
                "date": str(closest.get("timestamp", ""))[:8],
            }
            self._cache_put("wayback", url, lookup)
        snapshot_url = lookup.get("snapshot_url") or ""
        if not snapshot_url or not self._is_archive_url(snapshot_url):
            return "", "", None
        return snapshot_url, lookup.get("date") or "", self.page(raw_snapshot_url(snapshot_url))

    def _is_archive_url(self, snapshot_url: str) -> bool:
        """A snapshot must live on the archive we asked (archive.org, or the
        test double's host). A spoofed availability answer cannot redirect
        the fetcher to an arbitrary site."""
        host = (urllib.parse.urlsplit(snapshot_url).hostname or "").lower()
        api_host = (urllib.parse.urlsplit(self.wayback_api).hostname or "").lower()
        return host == api_host or host == "archive.org" or host.endswith(".archive.org")

    # ── disk cache (one JSON file per URL, written atomically) ──

    def _cache_path(self, kind: str, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(f"{kind}\n{url}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _cache_get(self, kind: str, url: str) -> dict[str, Any] | None:
        path = self._cache_path(kind, url)
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log(f"ignoring unreadable cache file {path}: {exc}")
            return None
        if not isinstance(data, dict) or data.get("cache_format") != CACHE_FORMAT \
                or data.get("kind") != kind or data.get("url") != url or not isinstance(data.get("data"), dict):
            return None
        return data["data"]

    def _cache_put(self, kind: str, url: str, payload: dict[str, Any]) -> None:
        path = self._cache_path(kind, url)
        if path is None or self.offline:
            return
        record = {"cache_format": CACHE_FORMAT, "kind": kind, "url": url,
                  "stored_at": _utc_now(), "data": payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            _log(f"could not write cache file {path}: {exc}")


_RESULT_FIELDS = {f.name for f in dataclasses.fields(FetchResult)}
_SNAPSHOT_PATH = re.compile(r"^(/web/\d{1,14})(?:[a-z]{2}_)?/")


def raw_snapshot_url(snapshot_url: str) -> str:
    """The Wayback URL for the capture as it was served, without the
    archive's toolbar or frame. Without `id_`, a PDF capture comes back as a
    short HTML wrapper whose text ("Wayback Machine ... captures ...") would
    be checked as if it were the page, and the claim reported absent."""
    parts = urllib.parse.urlsplit(snapshot_url)
    path = _SNAPSHOT_PATH.sub(r"\1id_/", parts.path, count=1)
    return parts._replace(path=path).geturl()


# ── Per-claim decision ──────────────────────────────────────────────────────

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
    unreadable = not live_failed and live.kind in ("pdf", "binary")
    too_thin = not live_failed and not unreadable and len(live.text.strip()) < MIN_PAGE_TEXT_CHARS
    base = {"http_status": status, "fetched_url": live.final_url or None}

    if (live_failed or too_thin) and fetcher.use_wayback:
        snapshot_url, snapshot_date, archived = fetcher.wayback(url)
        if archived is not None and archived.status is not None and archived.status < 400 and archived.text:
            tier, note, quoted, topical = _verdict_from_text(claim, archived, fetcher.policy.max_bytes)
            why = "unreachable" if live_failed else "yielded no text"
            suffix = f"checked against Wayback archive ({snapshot_date or 'undated'}), live page {why}"
            return Verdict(tier, f"{note} — {suffix}" if note else suffix, quoted, topical,
                           checked_against="wayback", snapshot_url=snapshot_url, **base)
        if live_failed and archived is not None and archived.status is not None and archived.status < 400 \
                and archived.kind in ("pdf", "binary"):
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


# ── Input ───────────────────────────────────────────────────────────────────

class InputError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def load_claims(source: str) -> list[dict[str, Any]]:
    """Read and validate the claims array. Raises InputError listing every
    problem found (up to 50), so a bad file is fixed in one pass."""
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
        extra = len(problems) - 50
        raise InputError(problems[:50] + ([f"... and {extra} more"] if extra > 0 else []))

    seen: dict[str, int] = {}
    for index, entry in enumerate(data):
        if "id" in entry:
            key = str(entry["id"])
            if key in seen:
                _log(f"warning: id {key!r} appears at [{seen[key]}] and [{index}]")
            seen.setdefault(key, index)
    return data


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return {dict: "object", list: "array", str: "string", int: "number", float: "number"}.get(type(value), "value")


# ── CLI ─────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    parser = argparse.ArgumentParser(
        prog="cited", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="JSON array of {id, claim, source_url}; - reads stdin")
    parser.add_argument("--version", action="version", version=f"cited {VERSION}")
    out = parser.add_argument_group("output")
    out.add_argument("--json", action="store_true",
                     help="print a machine-readable report (schema in README) to stdout instead of the table")
    out.add_argument("--annotate-out", type=Path, metavar="PATH",
                     help="write the input back out with verification_tier/verification_note/verified_at on every entry")
    out.add_argument("--strict", action="store_true",
                     help="also fail (exit 1) on unverified and snippet_only: claims that were never checked")
    net = parser.add_argument_group("fetching")
    net.add_argument("--timeout", type=_positive_float, default=10.0, metavar="SEC",
                     help="per-request time limit, connect through last byte (default 10)")
    net.add_argument("--retries", type=_int_range(0, 5), default=2,
                     help="retries for timeouts, connection errors, 429 and 5xx, with exponential backoff (default 2)")
    net.add_argument("--concurrency", type=_int_range(1, 32), default=4,
                     help="claims checked in parallel (default 4)")
    net.add_argument("--per-host-delay", type=_non_negative_float, default=1.0, metavar="SEC",
                     help="minimum gap between requests to the same host (default 1.0)")
    net.add_argument("--no-wayback", action="store_true",
                     help="do not fall back to the Wayback Machine for dead or empty pages")
    net.add_argument("--max-bytes", type=_int_range(10_000, 100_000_000), default=5_000_000,
                     help="read at most this many bytes per page (default 5000000)")
    net.add_argument("--max-redirects", type=_int_range(0, 20), default=5,
                     help="follow at most this many redirects (default 5)")
    cache = parser.add_argument_group("reproducible runs")
    cache.add_argument("--cache", type=Path, metavar="DIR",
                       help="read fetched pages from DIR when present, and store new ones there")
    cache.add_argument("--offline", action="store_true",
                       help="never touch the network; use only --cache (missing entries exit 3)")
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
        _log(f"internal error checking claim [{index}]: {type(exc).__name__}: {exc}")
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
            _log(f"could not write {args.annotate_out}: {exc.strerror or exc}")
            code = EXIT_BLOCKING if reason else EXIT_INTERNAL_ERROR

    if args.json:
        report = {
            "schema_version": SCHEMA_VERSION,
            "cited_version": VERSION,
            "checked_at": _utc_now(),
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
