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

Generalized from signal-scout's verify_sources.py (github.com/OrenSegal/
signal-scout) — same mechanism, decoupled from that project's specific
prospect schema so it works on any list of (claim, source_url) pairs: leads,
podcast quotes, research citations, changelog claims, anything you're about
to ship that cites a source.

Input schema — a JSON array of objects, each with:
    {"id": "<stable identifier>", "claim": "<text that should be on the page>",
     "source_url": "<https://...>"}

Usage:
    python3 verify_claims.py claims.json [--timeout 10] [--annotate-out OUT.json]

Exit code is non-zero if any source is unreachable (dead link, invalid URL),
any claim is missing from its live page, or any claim only shares vocabulary
with the page (low_match). A low match is not proof the claim is on the page,
so it needs a human to check it before the artifact ships.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tiering_core import (
    TIER_BROKEN,
    TIER_LABELS,
    TIER_LOW_MATCH,
    TIER_SNIPPET_ONLY,
    TIER_UNSUPPORTED,
    blocking_reason,
    tier_for_claim,
)

BOT_WALLED_DOMAINS = (
    "reddit.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "glassdoor.com",
    "indeed.com",
)


def bot_walled_host(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
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
    if parsed.netloc.lower() in ("reddit.com", "www.reddit.com"):
        return parsed._replace(netloc="old.reddit.com").geturl()
    return url


def is_challenge_page(status: int | None, text: str) -> bool:
    if status != 200 or len(text) > 200:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


_META_KEYS = frozenset({
    "description", "og:description", "og:title", "twitter:description", "twitter:title",
})


def _ldjson_strings(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(parsed)
    return " ".join(found)


class _TextExtractor(HTMLParser):
    """Visible text, plus meta descriptions and JSON-LD string values —
    the latter two are what a JS-rendered page still exposes to a plain
    fetch, and are often enough to verify a claim that would otherwise be
    Unverified."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False
        self._in_ldjson = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "script":
            attr_map = dict(attrs)
            self._in_ldjson = (attr_map.get("type") or "").strip().lower() == "application/ld+json"
            self._skip = not self._in_ldjson
        elif tag == "style":
            self._skip = True
        elif tag == "meta":
            attr_map = dict(attrs)
            key = (attr_map.get("name") or attr_map.get("property") or "").strip().lower()
            content = attr_map.get("content")
            if key in _META_KEYS and content:
                self._chunks.append(f" {content} ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
            self._in_ldjson = False

    def handle_data(self, data: str) -> None:
        if self._in_ldjson:
            self._chunks.append(f" {_ldjson_strings(data)} ")
        elif not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._chunks)).strip()


def fetch_text(url: str, timeout: int) -> tuple[int | None, str]:
    request = urllib.request.Request(
        canonicalize_for_fetch(url), headers={"User-Agent": "Mozilla/5.0 (cited)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(2_000_000).decode(charset, errors="ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        print(f"[cited] fetch failed for {url}: {exc}", file=sys.stderr)
        return None, ""
    parser = _TextExtractor()
    parser.feed(raw)
    return status, parser.text()


WAYBACK_API = "https://archive.org/wayback/available?url="


def fetch_wayback(url: str, timeout: int) -> tuple[str, str, str]:
    """Try the Wayback Machine for a page we couldn't read live."""
    try:
        request = urllib.request.Request(
            WAYBACK_API + urllib.parse.quote(url, safe=""),
            headers={"User-Agent": "Mozilla/5.0 (cited)"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            info = json.load(response)
        closest = (info.get("archived_snapshots") or {}).get("closest") or {}
        snapshot_url = closest.get("url") if closest.get("available") else ""
        snapshot_date = str(closest.get("timestamp", ""))[:8]
    except Exception as exc:
        print(f"[cited] wayback lookup failed for {url}: {exc}", file=sys.stderr)
        return "", "", ""
    if not snapshot_url:
        return "", "", ""
    status, text = fetch_text(snapshot_url, timeout)
    if status is not None and status < 400 and text:
        return snapshot_url, snapshot_date, text
    return "", "", ""


def check_source(url: str, claim: str, timeout: int) -> tuple[str, str, float, float]:
    status, page_text = fetch_text(url, timeout)

    live_failed = status is None or status >= 400
    too_thin = not live_failed and len(page_text.strip()) < 200
    if live_failed or too_thin:
        snapshot_url, snapshot_date, archive_text = fetch_wayback(url, timeout)
        if archive_text:
            tier, note, quoted, topical = tier_for_claim(claim, archive_text)
            suffix = f" — checked against Wayback archive ({snapshot_date or 'undated'}), live page {'unreachable' if live_failed else 'yielded no text'}"
            return tier, (note + suffix).strip(" —") if not note else note + suffix, quoted, topical

    if status == 429:
        return (TIER_SNIPPET_ONLY,
                "Source rate-limited the fetch (HTTP 429) and no archived copy found; claim is snippet-sourced only",
                0.0, 0.0)
    domain = bot_walled_host(url)
    if (status in (403, 429) or is_challenge_page(status, page_text)) and domain:
        return (TIER_SNIPPET_ONLY,
                f"{domain} blocks automated fetch and no archived copy found; claim is snippet-sourced only",
                0.0, 0.0)
    if live_failed:
        return TIER_BROKEN, f"Source unreachable (status {status}) and no archived copy found", 0.0, 0.0
    return tier_for_claim(claim, page_text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="JSON array of {id, claim, source_url}")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument(
        "--annotate-out",
        type=Path,
        help="Write the input JSON back out with verification_tier/verification_note/verified_at "
        "added to every entry (including broken ones) — for a report generator to render as a badge.",
    )
    args = parser.parse_args()

    entries = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        print("Input must be a JSON array of {id, claim, source_url} objects.", file=sys.stderr)
        sys.exit(2)

    results: list[tuple[str, str, str, float, float]] = []
    counts: dict[str, int] = {}

    for entry in entries:
        entry_id = str(entry.get("id", "(unnamed)"))
        url = str(entry.get("source_url", "")).strip()
        claim = str(entry.get("claim", ""))

        if not url.startswith(("http://", "https://")):
            tier, note, quoted, topical = TIER_BROKEN, "Invalid source_url", 0.0, 0.0
        else:
            tier, note, quoted, topical = check_source(url, claim, args.timeout)

        entry["verification_tier"] = tier
        entry["verification_note"] = note
        entry["verified_at"] = date.today().isoformat()
        counts[tier] = counts.get(tier, 0) + 1
        results.append((entry_id, url, tier, quoted, topical))

    print(f"{'ID':<26} {'TIER':<14} {'QUOTED':>6} {'TOPICAL':>7}  URL")
    for entry_id, url, tier, quoted, topical in results:
        label = TIER_LABELS.get(tier, tier)
        print(f"{entry_id[:26]:<26} {label:<14} {quoted:>6.2f} {topical:>7.2f}  {url}")

    total = len(results)
    summary = ", ".join(f"{n} {TIER_LABELS.get(t, t).lower()}" for t, n in sorted(counts.items())) or "none"
    print(f"\n{total} claims checked — {summary}.")

    unsupported = counts.get(TIER_UNSUPPORTED, 0)
    broken = counts.get(TIER_BROKEN, 0)
    if unsupported:
        print(
            f"\n{unsupported} claim(s) are NOT ON THE PAGE they cite. The source loaded and was "
            "readable, and the claim isn't in it — treat as fabricated until proven otherwise."
        )
    if broken:
        print(f"\n{broken} source(s) are unreachable or invalid. Drop the claim or find a working source.")
    low_match = counts.get(TIER_LOW_MATCH, 0)
    if low_match:
        print(
            f"\n{low_match} claim(s) only share vocabulary with their page and are not quoted from it. "
            "A human must check each one against the page, or tighten it to what the page says, before shipping."
        )

    if args.annotate_out:
        args.annotate_out.parent.mkdir(parents=True, exist_ok=True)
        args.annotate_out.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        print(f"\nAnnotated JSON written: {args.annotate_out.resolve()}")

    if blocking_reason(counts):
        sys.exit(1)


if __name__ == "__main__":
    main()
