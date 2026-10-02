"""How cited gets a source: retries, per-host spacing, one fetch per URL per
run, a disk cache for reproducible runs, the Wayback Machine lookup, and the
handling of sites that wall off scripted fetches. Every request goes through
safe_fetch.fetch_page."""

from __future__ import annotations

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
import urllib.parse
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from safe_fetch import FetchPolicy, FetchResult, fetch_page, url_problem

DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 2
DEFAULT_PER_HOST_DELAY = 1.0
MAX_RETRY_WAIT = 30.0

WAYBACK_API = "https://archive.org/wayback/available?url="
CACHE_FORMAT = 1

BOT_WALLED_DOMAINS = (
    "reddit.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "glassdoor.com",
    "indeed.com",
)
CHALLENGE_MARKERS = ("please wait for verification", "checking your browser")


def log(message: str) -> None:
    print(f"[cited] {message}", file=sys.stderr)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()


def bot_walled_host(url: str) -> str | None:
    host = host_of(url)
    for domain in BOT_WALLED_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def canonicalize_for_fetch(url: str) -> str:
    """reddit.com and www.reddit.com serve scripts a bot-verification stub;
    old.reddit.com serves the same page server-rendered. Only the fetch
    target is rewritten; the URL in the report is untouched."""
    if host_of(url) in ("reddit.com", "www.reddit.com"):
        parsed = urllib.parse.urlsplit(url)
        netloc = "old.reddit.com" + (f":{parsed.port}" if parsed.port else "")
        return parsed._replace(netloc=netloc).geturl()
    return url


def is_challenge_page(status: int | None, text: str) -> bool:
    if status != 200 or len(text) > 200:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


_SNAPSHOT_PATH = re.compile(r"^(/web/\d{1,14})(?:[a-z]{2}_)?/")


def raw_snapshot_url(snapshot_url: str) -> str:
    """The Wayback URL for the capture as it was served, without the
    archive's toolbar or frame. Without `id_`, a PDF capture comes back as a
    short HTML wrapper whose text would be checked as if it were the page."""
    parts = urllib.parse.urlsplit(snapshot_url)
    path = _SNAPSHOT_PATH.sub(r"\1id_/", parts.path, count=1)
    return parts._replace(path=path).geturl()


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


class DiskCache:
    """One JSON file per (kind, URL), written atomically. Trusted input:
    whoever can write the directory decides the results."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, kind: str, url: str) -> Path:
        digest = hashlib.sha256(f"{kind}\n{url}".encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, kind: str, url: str) -> dict[str, Any] | None:
        path = self._path(kind, url)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"ignoring unreadable cache file {path}: {exc}")
            return None
        if not isinstance(data, dict) or data.get("cache_format") != CACHE_FORMAT \
                or data.get("kind") != kind or data.get("url") != url or not isinstance(data.get("data"), dict):
            return None
        return data["data"]

    def put(self, kind: str, url: str, payload: dict[str, Any]) -> None:
        path = self._path(kind, url)
        record = {"cache_format": CACHE_FORMAT, "kind": kind, "url": url, "stored_at": utc_now(), "data": payload}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"could not write cache file {path}: {exc}")


_RESULT_FIELDS = {f.name for f in dataclasses.fields(FetchResult)}


class Fetcher:
    def __init__(self, *, policy: FetchPolicy | None = None, timeout: float = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES, backoff: float = 1.0, max_retry_wait: float = MAX_RETRY_WAIT,
                 per_host_delay: float = DEFAULT_PER_HOST_DELAY, cache_dir: Path | None = None,
                 offline: bool = False, use_wayback: bool = True,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.policy = policy or FetchPolicy()
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_retry_wait = max_retry_wait
        self.cache = DiskCache(cache_dir) if cache_dir is not None else None
        self.offline = offline
        self.use_wayback = use_wayback
        self.wayback_api = WAYBACK_API
        self._sleep = sleep
        self._throttle = HostThrottle(per_host_delay, sleep=sleep)
        self._memo: dict[tuple[str, str], Future] = {}
        self._lock = threading.Lock()

    def _once(self, key: tuple[str, str], compute: Callable[[], Any]) -> Any:
        """Every distinct URL is fetched at most once per run, even when
        several claims cite it and are checked concurrently."""
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

    def _cache_get(self, kind: str, url: str) -> dict[str, Any] | None:
        return self.cache.get(kind, url) if self.cache else None

    def _cache_put(self, kind: str, url: str, payload: dict[str, Any]) -> None:
        if self.cache and not self.offline:
            self.cache.put(kind, url, payload)

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
            log(f"fetch failed for {url}: {result.error}")
        if not result.blocked and not result.transient:
            self._cache_put("page", url, dataclasses.asdict(result))
        return result

    def _fetch_with_retries(self, url: str) -> FetchResult:
        host = host_of(url)
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
                return result
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
            lookup = self._wayback_lookup(url)
            if lookup is None:
                return "", "", None
            self._cache_put("wayback", url, lookup)
        snapshot_url = lookup.get("snapshot_url") or ""
        if not snapshot_url or not self._is_archive_url(snapshot_url):
            return "", "", None
        return snapshot_url, lookup.get("date") or "", self.page(raw_snapshot_url(snapshot_url))

    def _wayback_lookup(self, url: str) -> dict[str, str] | None:
        """The availability API's closest capture, or None if the lookup failed."""
        response = self._fetch_with_retries(self.wayback_api + urllib.parse.quote(url, safe=""))
        if response.status != 200 or not response.text:
            if response.error:
                log(f"wayback lookup failed for {url}: {response.error}")
            return None
        try:
            info = json.loads(response.text)
            closest = (info.get("archived_snapshots") or {}).get("closest") or {}
        except (ValueError, AttributeError) as exc:
            log(f"wayback lookup for {url} returned unreadable JSON: {exc}")
            return None
        snapshot_url = closest.get("url") if closest.get("available") else ""
        capture_status = str(closest.get("status") or "")
        if capture_status and not capture_status.startswith(("2", "3")):
            snapshot_url = ""  # a capture of an error page proves nothing
        return {
            "snapshot_url": snapshot_url if isinstance(snapshot_url, str) else "",
            "date": str(closest.get("timestamp", ""))[:8],
        }

    def _is_archive_url(self, snapshot_url: str) -> bool:
        """A snapshot must live on archive.org or the API's own host, so a
        spoofed availability answer cannot point the fetcher anywhere else."""
        host = host_of(snapshot_url)
        return host == host_of(self.wayback_api) or host == "archive.org" or host.endswith(".archive.org")
