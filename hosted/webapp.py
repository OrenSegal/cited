#!/usr/bin/env python3
"""cited, hosted: paste a Markdown draft (or link to one, or to a web page) and
get either a free link check or a paid, shareable certificate page. Standard
library only.

Routes:
  GET  /                  the upload page
  POST /check             mode=links: dead and redirected sources, first FREE_LINK_CAP links, nothing stored
                          mode=certificate: needs an access code; starts a certificate job and shows
                          its share link and private delete link
  GET  /c/{id}            a certificate. While its job runs, each visit checks the next batches of
                          claims within a time budget and the page refreshes itself until done
  GET  /c/{id}/delete     confirm deleting a certificate (needs its secret key)
  POST /c/{id}/delete     delete it
  POST /stripe/webhook    checkout.session.completed mints an access code (UNVERIFIED against live Stripe)
  GET  /healthz           liveness

Every fetch, including the draft URL itself, goes through cited's guarded
fetcher (safe_fetch): public addresses only, capped size, redirects and time.

Run locally:  python3 hosted/webapp.py   (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "cited" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from certificate import CSS as BASE_CSS  # noqa: E402
from certificate import render as render_certificate  # noqa: E402
from extract import extract  # noqa: E402
from fetcher import Fetcher, utc_now  # noqa: E402
from htmldraft import html_to_markdown  # noqa: E402
from page_text import decode_body  # noqa: E402
from safe_fetch import VERSION, FetchPolicy, FetchResult, fetch_page  # noqa: E402
from storage import Storage, StorageError, storage_from_env  # noqa: E402
from tiering_core import TIERS  # noqa: E402
from verify_claims import check_entries  # noqa: E402

FREE_LINK_CAP = 25
CERT_CLAIM_CAP = 150
MAX_FORM_BYTES = 1_000_000
MAX_DOC_BYTES = 500_000
FETCH_TIMEOUT = 8.0
CONCURRENCY = 8
PAYMENT_LINK_PLACEHOLDER = "STRIPE_PAYMENT_LINK_TODO"
CITED_HOME = "https://github.com/OrenSegal/cited"
CERT_ID = re.compile(r"[A-Za-z0-9_-]{12,32}")

# Certificate jobs. Vercel's Python runtime ends the work when the response is
# sent, so nothing runs in the background: each visit to a pending /c/{id}
# checks whole batches of BATCH_SIZE claims (one round of parallel fetches)
# and stops starting new ones once POLL_BUDGET would be overrun. A batch can
# take at most about JOB_WORST_BATCH seconds (live fetch with one retry, then
# a Wayback lookup and snapshot, each JOB_TIMEOUT), so no visit gets near the
# 60-second maxDuration.
BATCH_SIZE = CONCURRENCY
JOB_TIMEOUT = 6.0
JOB_RETRY_WAIT = 1.0
JOB_WORST_BATCH = 3 * (2 * JOB_TIMEOUT + JOB_RETRY_WAIT) + 2.0
POLL_BUDGET = 50.0
LEASE_SECONDS = 60  # a batch claimed by one visitor is not retried by another for this long
REFRESH_SECONDS = 1

RATE_LIMIT = (10, 600)  # POST /check: requests per window (seconds), per client address
STRIPE_TOLERANCE = 300  # seconds a webhook signature stays valid

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

CSS = BASE_CSS + """
.wrap{max-width:760px}
.lede a{white-space:nowrap}
label,.label{display:block;font-weight:600;font-size:15px;margin:22px 0 8px}
.hint{font-weight:400;color:var(--mut)}
textarea,input[type=url],input[type=text]{width:100%;font:15px/1.5 var(--mono);padding:12px 14px;
border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
textarea::placeholder,input::placeholder{color:var(--mut);opacity:.85}
textarea{min-height:200px;resize:vertical}
textarea:focus,input:focus{outline:2px solid var(--focus);outline-offset:1px;border-color:transparent}
.modes{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-top:8px;border:0;padding:0}
.modes legend{font-weight:600;font-size:15px;margin:22px 0 8px;padding:0}
.mode{position:relative;display:block;margin:0;padding:16px 16px 16px 46px;border:1px solid var(--line);border-radius:10px;
background:var(--card);cursor:pointer;font-weight:400}
.mode input{position:absolute;left:16px;top:19px;margin:0;accent-color:var(--fg);width:18px;height:18px}
.mode b{display:block;font-size:16px;margin-bottom:2px}
.mode .price{float:right;font-weight:600}
.mode span{display:block;color:var(--mut);font-size:14px}
.mode:has(input:checked){border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}
.mode:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:2px}
.code{margin-top:4px}
form:has(input[value=links]:checked) .code{display:none}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:14px 20px;margin-top:26px}
.btn{display:inline-block;font:600 16px var(--sans);padding:12px 22px;border:0;border-radius:8px;
background:var(--fg);color:var(--bg);cursor:pointer;text-decoration:none}
.btn:hover{background:color-mix(in srgb,var(--fg) 85%,var(--bg))}
.btn.quiet{background:transparent;color:var(--fg);box-shadow:inset 0 0 0 1px var(--line)}
.why{font-size:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:0 0 16px}
.panel p{margin:0 0 10px}.panel p:last-child{margin:0}
.secret{display:block;font:14px/1.5 var(--mono);background:var(--sunk);padding:10px 12px;border-radius:6px;
margin:6px 0 4px;overflow-wrap:anywhere;user-select:all}
.links{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.links li{display:grid;grid-template-columns:9.5em 1fr;gap:4px 14px;padding:12px 0;border-bottom:1px solid var(--line)}
.links .st{font-weight:600;color:var(--tc,var(--fg));font-size:14px}
.links .st::before{content:"";display:inline-block;width:9px;height:9px;border-radius:2px;background:var(--tc,var(--mut));
margin-right:8px;vertical-align:0}
.links a{overflow-wrap:anywhere;font-size:15px}
.l-dead,.l-refused{--tc:var(--bad)}.l-walled{--tc:var(--info)}.l-redirected{--tc:var(--warn)}.l-ok{--tc:var(--ok)}
.tally .l-dead,.tally .l-refused,.legend .l-dead i,.legend .l-refused i{background:var(--bad)}
.tally .l-walled,.legend .l-walled i{background:var(--info)}
.tally .l-redirected,.legend .l-redirected i{background:var(--warn)}
.tally .l-ok,.legend .l-ok i{background:var(--ok)}
.progress{height:10px;border-radius:5px;background:var(--sunk);overflow:hidden;margin:18px 0 10px}
.progress span{display:block;height:100%;background:var(--fg)}
@media (max-width:520px){.links li{grid-template-columns:1fr}.mode .price{float:none}}
@media (prefers-reduced-motion:no-preference){.progress span{transition:width .4s cubic-bezier(.16,1,.3,1)}}
"""

LINK_STATUS = {
    "dead": "Dead",
    "refused": "Refused",
    "walled": "Blocks checkers",
    "redirected": "Redirected",
    "ok": "OK",
}
LINK_ORDER = tuple(LINK_STATUS)


class UserError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def _href(url: str) -> str:
    if url.lower().startswith(("http://", "https://")):
        return f'<a href="{_esc(url)}">{_esc(url)}</a>'
    return _esc(url)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _page(title: str, body: str, status: int = 200, head: str = "") -> Response:
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>{head}<style>{CSS}</style></head>
<body><main class="wrap">{body}
<footer>Checked by <a href="{CITED_HOME}">cited</a> {_esc(VERSION)}. cited checks that a claim's words are on the
page it cites. That is containment, not truth.</footer>
</main></body></html>
"""
    return Response(status, doc.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8",
                                                  "Cache-Control": "no-store", **SECURITY_HEADERS})


def _same_url(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        return urllib.parse.urldefrag(url)[0].rstrip("/")
    return norm(a) == norm(b)


def link_status(url: str, result: FetchResult) -> tuple[str, str]:
    """(status key, detail) for one fetched link in the free check."""
    if result.blocked:
        return "refused", "Not fetched: " + result.error.removeprefix("refused: ")
    if result.status is None:
        return "dead", result.error or "no response"
    if result.status in (401, 403, 429):
        return "walled", f"HTTP {result.status}: the site refuses automated readers, so this proves nothing either way"
    if result.status >= 400:
        return "dead", f"HTTP {result.status}"
    if result.final_url and not _same_url(url, result.final_url):
        return "redirected", f"Now at {result.final_url}"
    return "ok", f"HTTP {result.status}"


class RateLimiter:
    """Fixed-window limit per client address. With storage, each request
    claims one numbered slot key with `create` (first writer wins), so the
    count is shared by every function instance. Without storage it counts in
    this process only. Addresses are stored hashed."""

    def __init__(self, storage: Storage | None, limit: int, window: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.storage, self.limit, self.window, self.clock = storage, limit, window, clock
        self._next: dict[tuple[str, int], int] = {}  # first slot worth trying, per (client, window)
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        if not client or self.limit <= 0:
            return True
        bucket = int(self.clock() // self.window)
        who = _sha(f"cited-rl:{client}")[:32]
        with self._lock:
            for key in [k for k in self._next if k[1] < bucket]:
                del self._next[key]
            start = self._next.get((who, bucket), 0)
        if self.storage is None:
            with self._lock:
                self._next[(who, bucket)] = start + 1
            return start < self.limit
        for slot in range(start, self.limit):
            try:
                won = self.storage.create(f"ratelimit/{bucket}/{who}/{slot}", b"")
            except StorageError as exc:
                print(f"cited: rate limit storage unavailable, allowing: {exc}", file=sys.stderr)
                return True
            if won:
                with self._lock:
                    self._next[(who, bucket)] = slot + 1
                return True
        with self._lock:
            self._next[(who, bucket)] = self.limit
        return False


def stripe_signature_ok(payload: bytes, header: str, secret: str, now: float, tolerance: int = STRIPE_TOLERANCE) -> bool:
    """Stripe's scheme: HMAC-SHA256 of "{t}.{payload}" with the endpoint secret, in one or more v1= fields."""
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        stamp = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if abs(now - stamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


def email_access_code(email: str, code: str) -> bool:
    """Placeholder: no email provider is wired up. Returns False so the order
    record keeps the code for the owner to send by hand."""
    print(f"cited: TODO email an access code to {email or 'an unknown buyer'} (not sent)", file=sys.stderr)
    return False


class App:
    def __init__(self, *, storage: Storage | None = None, storage_error: str | None = None,
                 policy: FetchPolicy | None = None, access_codes: frozenset[str] = frozenset(),
                 payment_link: str = PAYMENT_LINK_PLACEHOLDER, timeout: float = FETCH_TIMEOUT,
                 job_timeout: float = JOB_TIMEOUT, per_host_delay: float = 0.25, use_wayback: bool = True,
                 poll_budget: float = POLL_BUDGET, worst_batch: float = JOB_WORST_BATCH,
                 rate_limit: tuple[int, int] = RATE_LIMIT, stripe_secret: str = "",
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.storage = storage
        self.storage_error = storage_error
        self.policy = policy or FetchPolicy()
        self.access_codes = access_codes
        self.payment_link = payment_link
        self.timeout = timeout
        self.job_timeout = job_timeout
        self.per_host_delay = per_host_delay
        self.use_wayback = use_wayback
        self.poll_budget = poll_budget
        self.worst_batch = worst_batch
        self.limiter = RateLimiter(storage, *rate_limit, clock=clock)
        self.stripe_secret = stripe_secret
        self.clock = clock
        self.monotonic = monotonic

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> App:
        env = dict(os.environ) if env is None else env
        storage, error = None, None
        try:
            storage = storage_from_env(env)
        except StorageError as exc:
            error = str(exc)
        codes = frozenset(c.strip() for c in env.get("CITED_ACCESS_CODES", "").split(",") if c.strip())
        limit = RATE_LIMIT
        if env.get("CITED_RATE_LIMIT", "").strip():
            count, _, window = env["CITED_RATE_LIMIT"].partition("/")
            limit = (int(count), int(window or RATE_LIMIT[1]))
        return cls(storage=storage, storage_error=error, access_codes=codes, rate_limit=limit,
                   payment_link=env.get("CITED_PAYMENT_LINK", "").strip() or PAYMENT_LINK_PLACEHOLDER,
                   stripe_secret=env.get("STRIPE_WEBHOOK_SECRET", "").strip())

    # Routing

    def handle(self, method: str, path: str, body: bytes = b"", content_type: str = "",
               client_ip: str = "", headers: dict[str, str] | None = None) -> Response:
        parts = urllib.parse.urlsplit(path)
        route, query = parts.path, urllib.parse.parse_qs(parts.query)
        try:
            if route == "/":
                return self._only(method, "GET") or self.landing()
            if route == "/healthz":
                return self._only(method, "GET") or Response(200, b"ok\n", {"Content-Type": "text/plain"})
            if route == "/check":
                if self._only(method, "POST"):
                    return self._only(method, "POST")
                if not self.limiter.allow(client_ip):
                    return self.error("Too many checks from your address in the last few minutes. "
                                      "Wait ten minutes and try again.", 429, {"Retry-After": str(self.limiter.window)})
                return self.check(body, content_type)
            if route == "/stripe/webhook":
                return self._only(method, "POST") or self.stripe_webhook(body, (headers or {}).get("stripe-signature", ""))
            if route.startswith("/c/") and route.endswith("/delete"):
                cert_id = route[3:-len("/delete")]
                if method == "POST":
                    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                    return self.delete(cert_id, form.get("key", [""])[0], confirm=True)
                return self._only(method, "GET") or self.delete(cert_id, query.get("key", [""])[0], confirm=False)
            if route.startswith("/c/"):
                return self._only(method, "GET") or self.certificate(route[3:])
            return _page("Not found", "<h1>Not found</h1>", 404)
        except UserError as exc:
            return self.error(str(exc), exc.status)
        except StorageError as exc:
            print(f"cited: storage error: {exc}", file=sys.stderr)
            return self.error("Certificate storage is not answering right now. Nothing was lost; "
                              "try again in a minute.", 503)
        except Exception:  # noqa: BLE001 — a bug must become a page, not a dropped connection
            traceback.print_exc(file=sys.stderr)
            return self.error("Something went wrong on our side. Try again; if it keeps happening, "
                              "the draft may have hit a bug.", 500)

    @staticmethod
    def _only(method: str, allowed: str) -> Response | None:
        if method == allowed or (allowed == "GET" and method == "HEAD"):
            return None
        response = _page("Method not allowed", "<h1>Method not allowed</h1>", 405)
        response.headers["Allow"] = allowed
        return response

    def error(self, message: str, status: int = 400, headers: dict[str, str] | None = None) -> Response:
        response = _page("Could not check", f"""<h1>Could not check this</h1>
<p class="lede">{_esc(message)}</p>
<div class="actions"><a class="btn quiet" href="/">Back to the form</a></div>""", status)
        response.headers.update(headers or {})
        return response

    # Pages

    def landing(self) -> Response:
        return _page("cited: check every source in a draft", f"""<h1>Check every source in a draft</h1>
<p class="lede">Paste a draft, or link to one. cited re-fetches every page it cites and tells you which
sources are dead, and which claims are not on the page they cite.</p>
<form method="post" action="/check">
  <label for="markdown">Your draft <span class="hint">in Markdown</span></label>
  <textarea id="markdown" name="markdown" spellcheck="false"
    placeholder="Revenue grew 40% in 2025, [per the annual report](https://example.com/report)."></textarea>
  <label for="url">Or a link to it <span class="hint">a raw Markdown file or a web page</span></label>
  <input id="url" name="url" type="url" inputmode="url" placeholder="https://example.com/blog/your-post">
  <fieldset class="modes">
    <legend>What to check</legend>
    <label class="mode"><input type="radio" name="mode" value="links" checked>
      <b>Link check <span class="price">Free</span></b>
      <span>Which sources are dead, moved, or blocking readers. First {FREE_LINK_CAP} links, nothing stored.</span></label>
    <label class="mode"><input type="radio" name="mode" value="certificate">
      <b>Certificate <span class="price">$9</span></b>
      <span>Every claim checked against its page, on a page you can share. You can delete it any time.</span></label>
  </fieldset>
  <div class="code">
    <label for="code">Access code <span class="hint">for a certificate</span></label>
    <input id="code" name="code" type="text" autocomplete="off" autocapitalize="off" spellcheck="false">
    <p class="why">No code yet? <a href="{_esc(self.payment_link)}">Buy a certificate</a> and we will email you one.</p>
  </div>
  <div class="actions"><button class="btn" type="submit">Check sources</button>
  <span class="why">Big drafts take a minute.</span></div>
</form>
<p class="note">A certificate shows whether each claim's own words, numbers and names are on the page it cites.
It does not show the page is right.</p>""")

    def _cert_status(self, cert_id: str) -> tuple[bytes | None, dict[str, Any] | None, bool]:
        """(finished html, job meta, deleted) for a valid id."""
        storage = self._storage()
        if storage.get(f"certs/{cert_id}.deleted") is not None:
            return None, None, True
        data = storage.get(f"certs/{cert_id}.html")
        if data is not None:
            return data, None, False
        meta = storage.get(f"jobs/{cert_id}.json")
        return None, json.loads(meta) if meta is not None else None, False

    def certificate(self, cert_id: str) -> Response:
        if not CERT_ID.fullmatch(cert_id):
            return _page("Not found", "<h1>No such certificate</h1>", 404)
        data, meta, deleted = self._cert_status(cert_id)
        if deleted:
            return _page("Deleted", "<h1>This certificate was deleted</h1>"
                         '<p class="lede">Its owner removed it. Nothing about it is kept.</p>', 410)
        if data is None and meta is not None:
            done, total = self.advance(cert_id, meta)
            if done < total:
                return self.progress_page(meta, done, total)
            data = self.storage.get(f"certs/{cert_id}.html") if self.storage else None
        if data is None:
            return _page("Not found", "<h1>No such certificate</h1>", 404)
        return Response(200, data, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "private, max-age=60", **SECURITY_HEADERS})

    def progress_page(self, meta: dict[str, Any], done: int, total: int) -> Response:
        claims = len(meta["entries"])
        checked = min(claims, done * BATCH_SIZE)
        pct = round(100 * done / total) if total else 0
        return _page("Checking sources", f"""<h1>Checking {claims} claim(s)</h1>
<p class="lede">{checked} of {claims} checked. This page updates itself and turns into the certificate
when every source has been read.</p>
<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="{claims}" aria-valuenow="{checked}">
<span style="width:{pct}%"></span></div>
<p class="sub">Source check of <code>{_esc(meta['name'])}</code>. You can close this tab: checking carries on
from where it stopped the next time anyone opens this link.</p>""",
                     head=f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">')

    # Checking

    def check(self, body: bytes, content_type: str) -> Response:
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            raise UserError("Send the form from the upload page.", 415)
        if len(body) > MAX_FORM_BYTES:
            raise UserError(f"That is more than {MAX_FORM_BYTES:,} bytes. Check a shorter draft.", 413)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()}
        name, markdown = self.load_document(form.get("markdown", ""), form.get("url", "").strip())
        if form.get("mode") == "certificate":
            return self.start_certificate(name, markdown, form.get("code", "").strip())
        return self.run_link_check(name, markdown)

    def load_document(self, markdown: str, url: str) -> tuple[str, str]:
        if markdown.strip() and url:
            raise UserError("Paste a draft or give a link, not both.")
        if markdown.strip():
            if len(markdown.encode("utf-8")) > MAX_DOC_BYTES:
                raise UserError(f"Drafts are limited to {MAX_DOC_BYTES:,} bytes.", 413)
            return "pasted draft", markdown
        if not url:
            raise UserError("Paste a draft, or give a link to one.")
        result = fetch_page(url, self.timeout, replace(self.policy, max_bytes=MAX_DOC_BYTES), keep_body=True)
        if result.blocked:
            raise UserError("That URL is not on a public address, so it was not fetched.")
        if result.status is None or result.status >= 400:
            raise UserError(f"Could not fetch that URL ({result.error or f'HTTP {result.status}'}).")
        if result.truncated:
            raise UserError(f"That page is larger than {MAX_DOC_BYTES:,} bytes.", 413)
        final = result.final_url or url
        name = urllib.parse.urlsplit(final).path.rsplit("/", 1)[-1] or urllib.parse.urlsplit(final).netloc or url
        text = decode_body(result.extra["body"], result.extra.get("charset"))
        if result.kind == "html":
            title, text = html_to_markdown(text, final)
            return title or name, text
        if result.kind != "text":
            raise UserError("That link is not a web page or a text file (it looks like a PDF or other binary). "
                            "Paste the draft instead.")
        return name, text

    def run_link_check(self, name: str, markdown: str) -> Response:
        urls = list(dict.fromkeys(
            p.source_url for p in extract(markdown) if p.source_url.lower().startswith(("http://", "https://"))))
        checked, skipped = urls[:FREE_LINK_CAP], len(urls) - FREE_LINK_CAP
        fetcher = Fetcher(policy=self.policy, timeout=self.timeout, retries=0,
                          per_host_delay=self.per_host_delay, use_wayback=False)
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            results = list(pool.map(fetcher.page, checked))
        statuses = [(url, *link_status(url, result)) for url, result in zip(checked, results, strict=True)]
        order = {key: i for i, key in enumerate(LINK_ORDER)}
        statuses.sort(key=lambda s: order[s[1]])

        counts = {key: sum(1 for s in statuses if s[1] == key) for key in LINK_ORDER}
        problems = counts["dead"] + counts["refused"]
        if not statuses:
            headline, tone = "No links found", "warn"
        elif problems:
            headline, tone = f"{problems} of {len(statuses)} source(s) did not load", "bad"
        elif counts["walled"] or counts["redirected"]:
            headline, tone = "Every source answered, some need a look", "warn"
        else:
            headline, tone = f"All {len(statuses)} source(s) load", "ok"
        tally = "".join(f'<span class="l-{k}" style="flex:{n}"></span>' for k, n in counts.items() if n)
        legend = "".join(f'<li class="l-{k}"><i></i>{LINK_STATUS[k]} <b>{n}</b></li>' for k, n in counts.items() if n)
        rows = "".join(f'<li class="l-{key}"><span class="st">{_esc(LINK_STATUS[key])}</span>'
                       f'<div>{_href(url)}<div class="why">{_esc(detail)}</div></div></li>'
                       for url, key, detail in statuses)
        capped = (f'<p class="panel"><b>{skipped} more link(s) were not checked.</b> The free check covers the first '
                  f"{FREE_LINK_CAP}. A certificate checks every link.</p>") if skipped > 0 else ""
        listing = (f'<ol class="links">{rows}</ol>' if rows else
                   '<p class="lede">No http(s) links were found in this draft. cited reads Markdown links, '
                   "bare URLs and table rows that end in a URL.</p>")
        return _page(f"Link check: {name}", f"""<h1 class="v-{tone}">{_esc(headline)}</h1>
<p class="sub">Link check of <code>{_esc(name)}</code> · {len(checked)} link(s) checked · {_esc(utc_now())}</p>
{f'<div class="tally" role="img" aria-label="link statuses">{tally}</div><ul class="legend">{legend}</ul>' if tally else ''}
{capped}
{listing}
<h2>Want every claim checked?</h2>
<p class="meaning">This only shows whether each source still loads. A certificate also checks whether each claim's
own words, numbers and names are on the page it cites, and gives you a page to share.</p>
<div class="actions"><a class="btn" href="{_esc(self.payment_link)}">Get a certificate for $9</a>
<a href="/">Check another draft</a></div>""")

    def _storage(self) -> Storage:
        if self.storage is None:
            raise UserError(f"Certificates are unavailable: {self.storage_error or 'no storage configured'}.", 503)
        return self.storage

    def _code_ok(self, code: str) -> bool:
        # Compare against every code so timing does not reveal which one matched.
        matched = False
        for candidate in self.access_codes:
            matched |= hmac.compare_digest(candidate.encode(), code.encode())
        if not matched and self.storage is not None and code:
            matched = self.storage.get(f"minted/{_sha(code)}") is not None
        return matched

    def _payment_page(self, code: str) -> Response:
        return _page("Access code needed", f"""<h1>A certificate needs an access code</h1>
<p class="lede">{'That code is not valid. ' if code else ''}Certificates are $9 each. Buy one and we will
email you a code. Your draft is not stored, so paste it again when you have the code.</p>
<div class="actions"><a class="btn" href="{_esc(self.payment_link)}">Buy a certificate, $9</a>
<a href="/">Run the free link check instead</a></div>""", 402)

    def start_certificate(self, name: str, markdown: str, code: str) -> Response:
        if not self.access_codes and not self.stripe_secret:
            raise UserError("Certificates are not on sale yet.", 503)
        if not code:
            return self._payment_page(code)
        storage = self._storage()
        if not self._code_ok(code):
            return self._payment_page(code)

        pairs = extract(markdown)
        entries = [{"id": p.id, "claim": p.claim, "source_url": p.source_url, "line": p.line, "bucket": p.bucket}
                   for p in pairs if p.bucket != "excluded"]
        excluded = [{"id": p.id, "line": p.line, "link_text": p.link_text, "source_url": p.source_url,
                     "reason": p.reason} for p in pairs if p.bucket == "excluded"]
        if not entries:
            raise UserError("No checkable claims were found. A claim is a sentence with a link or URL to its source.")
        if len(entries) > CERT_CLAIM_CAP:
            raise UserError(f"This draft has {len(entries)} checkable claims; a certificate covers up to {CERT_CLAIM_CAP}.")

        # Single use: the first request to create this marker wins. Redeemed
        # only after the input is known to be checkable.
        cert_id = secrets.token_urlsafe(12)
        delete_key = secrets.token_urlsafe(24)
        if not storage.create(f"codes/{_sha(code)}", cert_id.encode()):
            raise UserError("That access code has already been used.", 409)
        meta = {"name": name, "entries": entries, "excluded": excluded, "links_total": len(pairs),
                "batch_size": BATCH_SIZE, "created": utc_now(), "delete_hash": _sha(delete_key)}
        storage.put(f"jobs/{cert_id}.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"), "application/json")
        share, delete = f"/c/{cert_id}", f"/c/{cert_id}/delete?key={delete_key}"
        return _page("Certificate started", f"""<h1>Your certificate is on its way</h1>
<p class="lede">{len(entries)} claim(s) to check. Open the certificate to watch it fill in; it takes about
a minute for every 40 sources.</p>
<div class="panel"><p><b>Share link.</b> Anyone with it can read the certificate.</p>
<a class="secret" href="{share}">{share}</a></div>
<div class="panel"><p><b>Private delete link.</b> Save it now: it is shown only once and cannot be recovered.
Anyone with it can delete the certificate.</p>
<a class="secret" href="{_esc(delete)}">{_esc(delete)}</a></div>
<div class="actions"><a class="btn" href="{share}">Open the certificate</a></div>""",
                     head='<meta name="robots" content="noindex">')

    def _batch_fetcher(self) -> Fetcher:
        return Fetcher(policy=self.policy, timeout=self.job_timeout, retries=1, backoff=0.5,
                       max_retry_wait=JOB_RETRY_WAIT, per_host_delay=self.per_host_delay,
                       use_wayback=self.use_wayback)

    def advance(self, cert_id: str, meta: dict[str, Any]) -> tuple[int, int]:
        """Check pending batches within the poll budget; finish the certificate
        when every batch is in. Returns (batches done, batches total)."""
        storage = self._storage()
        entries = meta["entries"]
        size = meta.get("batch_size", BATCH_SIZE)
        total = max(1, math.ceil(len(entries) / size))
        results: dict[int, list[dict[str, Any]]] = {}
        for n in range(total):
            data = storage.get(f"jobs/{cert_id}/b{n}.json")
            if data is not None:
                results[n] = json.loads(data)
        began, ran = self.monotonic(), 0
        for n in range(total):
            if n in results:
                continue
            if ran and self.monotonic() - began + self.worst_batch > self.poll_budget:
                break
            slot = int(self.clock() // LEASE_SECONDS)
            if not storage.create(f"jobs/{cert_id}/lease-b{n}-{slot}", b""):
                continue  # another visitor is checking this batch right now
            chunk = [dict(e) for e in entries[n * size:(n + 1) * size]]
            rows, _, _ = check_entries(chunk, self._batch_fetcher(), concurrency=CONCURRENCY, draft=True)
            for offset, row in enumerate(rows):
                row["index"] = n * size + offset
            storage.put(f"jobs/{cert_id}/b{n}.json", json.dumps(rows, ensure_ascii=False).encode("utf-8"),
                        "application/json")
            results[n] = rows
            ran += 1
        if len(results) == total:
            self.finish(cert_id, meta, [row for n in range(total) for row in results[n]])
        return len(results), total

    def finish(self, cert_id: str, meta: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        storage = self._storage()
        if storage.get(f"certs/{cert_id}.deleted") is not None:
            return
        counts = {tier: 0 for tier in TIERS}
        for row in rows:
            counts[row["tier"]] = counts.get(row["tier"], 0) + 1
        checked_at = utc_now()
        page = render_certificate(meta["name"], rows, checked_at, excluded=meta["excluded"],
                                  links_total=meta["links_total"], version=VERSION, footer_link=CITED_HOME)
        report = {"cited_version": VERSION, "checked_at": checked_at, "document": meta["name"],
                  "counts": counts, "results": rows, "excluded": meta["excluded"]}
        storage.put(f"certs/{cert_id}.json", json.dumps(report, ensure_ascii=False).encode("utf-8"),
                    "application/json")
        storage.put(f"certs/{cert_id}.html", page.encode("utf-8"), "text/html; charset=utf-8")

    # Owner actions

    def delete(self, cert_id: str, key: str, *, confirm: bool) -> Response:
        if not CERT_ID.fullmatch(cert_id):
            return _page("Not found", "<h1>No such certificate</h1>", 404)
        storage = self._storage()
        raw = storage.get(f"jobs/{cert_id}.json")
        if raw is None:
            gone = storage.get(f"certs/{cert_id}.deleted") is not None
            return _page("Not found", f"<h1>{'Already deleted' if gone else 'No such certificate'}</h1>", 410 if gone else 404)
        meta = json.loads(raw)
        if not key or not hmac.compare_digest(_sha(key), meta.get("delete_hash", "")):
            return _page("Wrong key", '<h1>That delete link is not valid</h1><p class="lede">Use the private '
                         "delete link shown when the certificate was made.</p>", 403)
        if not confirm:
            return _page("Delete certificate", f"""<h1>Delete this certificate?</h1>
<p class="lede">The share link will stop working for everyone, and the stored results are removed.
This cannot be undone.</p>
<form method="post" action="/c/{cert_id}/delete"><input type="hidden" name="key" value="{_esc(key)}">
<div class="actions"><button class="btn" type="submit">Delete it</button>
<a href="/c/{cert_id}">Keep it</a></div></form>""", head='<meta name="robots" content="noindex">')
        storage.put(f"certs/{cert_id}.deleted", b"", "text/plain")
        total = max(1, math.ceil(len(meta["entries"]) / meta.get("batch_size", BATCH_SIZE)))
        for item in [f"certs/{cert_id}.html", f"certs/{cert_id}.json",
                     *(f"jobs/{cert_id}/b{n}.json" for n in range(total)), f"jobs/{cert_id}.json"]:
            storage.delete(item)
        return _page("Deleted", '<h1>Deleted</h1><p class="lede">The certificate and its results are gone. '
                     "The share link now says it was deleted.</p>")

    def stripe_webhook(self, body: bytes, signature: str) -> Response:
        """checkout.session.completed: mint a single-use access code. UNVERIFIED
        against live Stripe; tested only with a fixture payload."""
        def reply(status: int, message: str) -> Response:
            return Response(status, json.dumps({"message": message}).encode(), {"Content-Type": "application/json"})

        if not self.stripe_secret:
            return reply(503, "webhook not configured")
        if not stripe_signature_ok(body, signature, self.stripe_secret, self.clock()):
            return reply(400, "bad signature")
        try:
            event = json.loads(body)
        except ValueError:
            return reply(400, "bad payload")
        if event.get("type") != "checkout.session.completed":
            return reply(200, "ignored")
        session = (event.get("data") or {}).get("object") or {}
        session_id = str(session.get("id") or "")
        if not re.fullmatch(r"cs_[A-Za-z0-9_]{1,200}", session_id):
            return reply(400, "no session id")
        if session.get("payment_status") not in (None, "paid", "no_payment_required"):
            return reply(200, "not paid")
        storage = self._storage()
        code = secrets.token_urlsafe(9)
        email = str((session.get("customer_details") or {}).get("email") or "")
        order = {"session": session_id, "email": email, "code": code, "created": utc_now(), "emailed": False}
        if not storage.create(f"orders/{session_id}.json", json.dumps(order).encode()):
            return reply(200, "already handled")  # Stripe retries; one code per session
        storage.put(f"minted/{_sha(code)}", session_id.encode(), "text/plain")
        if email_access_code(email, code):
            storage.put(f"orders/{session_id}.json", json.dumps({**order, "emailed": True}).encode(), "application/json")
        return reply(200, "code minted")


_app: App | None = None


def get_app() -> App:
    global _app
    if _app is None:
        _app = App.from_env()
    return _app


class Handler(BaseHTTPRequestHandler):
    """Serves `get_app()`. Used by the local server below and, as `handler`,
    by the Vercel Python runtime (api/index.py)."""

    server_version = "cited-hosted"

    def _client_ip(self) -> str:
        # On Vercel the platform sets these and overwrites anything a client
        # sent; anywhere else they are client-controlled, so ignore them.
        if os.environ.get("VERCEL"):
            forwarded = self.headers.get("X-Real-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0]
            if forwarded.strip():
                return forwarded.strip()
        return self.client_address[0] if self.client_address else ""

    def _serve(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_FORM_BYTES:
            response = get_app().error(f"Requests are limited to {MAX_FORM_BYTES:,} bytes.", 413)
            self.close_connection = True
        else:
            body = self.rfile.read(length) if length else b""
            response = get_app().handle(method, self.path, body, self.headers.get("Content-Type", ""),
                                        client_ip=self._client_ip(),
                                        headers={k.lower(): v for k, v in self.headers.items()})
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(response.body)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_HEAD(self) -> None:
        self._serve("HEAD")

    def do_POST(self) -> None:
        self._serve("POST")


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"cited hosted on http://127.0.0.1:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
