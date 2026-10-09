#!/usr/bin/env python3
"""cited, hosted: paste a Markdown draft (or link to one) and get either a
free link check or a paid, shareable certificate page. Standard library only.

Routes:
  GET  /          the upload page
  POST /check     mode=links: dead and redirected sources, first FREE_LINK_CAP links, nothing stored
                  mode=certificate: needs an access code; checks every claim, stores the
                  certificate and redirects to it
  GET  /c/{id}    a stored certificate
  GET  /healthz   liveness

Every fetch, including the draft URL itself, goes through cited's guarded
fetcher (safe_fetch): public addresses only, capped size, redirects and time.

Run locally:  python3 hosted/webapp.py   (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "cited" / "scripts"))

from certificate import render as render_certificate  # noqa: E402
from extract import extract  # noqa: E402
from fetcher import Fetcher, utc_now  # noqa: E402
from page_text import decode_body  # noqa: E402
from safe_fetch import VERSION, FetchPolicy, FetchResult, fetch_page  # noqa: E402
from storage import Storage, StorageError, storage_from_env  # noqa: E402
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

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

CSS = """
:root{--bg:#fbfaf8;--fg:#16150f;--mut:#6a6558;--line:#e2ded4;--card:#fff;--ok:#1a7f4b;--warn:#8a6100;
--bad:#a32020;--info:#4a4a8a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:40px 16px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:30px 0 10px;font-weight:600}
.sub{color:var(--mut);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:18px 20px;margin-bottom:14px}
label{display:block;font-weight:600;margin:12px 0 6px}
textarea,input[type=url],input[type=text]{width:100%;font:14px ui-monospace,SFMono-Regular,Menlo,monospace;
padding:10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--fg)}
textarea{min-height:220px;resize:vertical}
.modes{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-top:8px}
.mode{border:1px solid var(--line);border-radius:8px;padding:12px 14px;font-weight:400;margin:0;cursor:pointer}
.mode b{display:block}
.mode span{color:var(--mut);font-size:13.5px}
button{margin-top:16px;font:600 15px inherit;padding:10px 18px;border:0;border-radius:6px;background:var(--fg);color:#fff;cursor:pointer}
.why{color:var(--mut);font-size:13.5px}
.err{border-left:5px solid var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
.scroll{overflow-x:auto}
td,th{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td a{color:var(--info);word-break:break-all}
.tag{font-size:11.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:2px 8px;border-radius:4px;white-space:nowrap}
.s-ok{background:#e4f4ea;color:var(--ok)}
.s-redirected,.s-walled{background:#fdf1dc;color:var(--warn)}
.s-dead,.s-refused{background:#fbe6e6;color:var(--bad)}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--mut)}
footer a{color:var(--info)}
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


def _page(title: str, body: str, status: int = 200) -> Response:
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">{body}
<footer>Checked by <a href="{CITED_HOME}">cited</a> {_esc(VERSION)}. cited checks that a claim's words are on the
page it cites. That is containment, not truth.</footer>
</div></body></html>
"""
    return Response(status, doc.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8", **SECURITY_HEADERS})


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


class App:
    def __init__(self, *, storage: Storage | None = None, storage_error: str | None = None,
                 policy: FetchPolicy | None = None, access_codes: frozenset[str] = frozenset(),
                 payment_link: str = PAYMENT_LINK_PLACEHOLDER, timeout: float = FETCH_TIMEOUT,
                 per_host_delay: float = 0.25, use_wayback: bool = True) -> None:
        self.storage = storage
        self.storage_error = storage_error
        self.policy = policy or FetchPolicy()
        self.access_codes = access_codes
        self.payment_link = payment_link
        self.timeout = timeout
        self.per_host_delay = per_host_delay
        self.use_wayback = use_wayback

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> App:
        env = dict(os.environ) if env is None else env
        storage, error = None, None
        try:
            storage = storage_from_env(env)
        except StorageError as exc:
            error = str(exc)
        codes = frozenset(c.strip() for c in env.get("CITED_ACCESS_CODES", "").split(",") if c.strip())
        return cls(storage=storage, storage_error=error, access_codes=codes,
                   payment_link=env.get("CITED_PAYMENT_LINK", "").strip() or PAYMENT_LINK_PLACEHOLDER)

    # Routing

    def handle(self, method: str, path: str, body: bytes = b"", content_type: str = "") -> Response:
        route = urllib.parse.urlsplit(path).path
        try:
            if route == "/":
                return self._only(method, "GET") or self.landing()
            if route == "/healthz":
                return self._only(method, "GET") or Response(200, b"ok\n", {"Content-Type": "text/plain"})
            if route == "/check":
                return self._only(method, "POST") or self.check(body, content_type)
            if route.startswith("/c/"):
                return self._only(method, "GET") or self.certificate(route[3:])
            return _page("Not found", "<h1>Not found</h1>", 404)
        except UserError as exc:
            return self.error(str(exc), exc.status)

    @staticmethod
    def _only(method: str, allowed: str) -> Response | None:
        if method == allowed or (allowed == "GET" and method == "HEAD"):
            return None
        response = _page("Method not allowed", "<h1>Method not allowed</h1>", 405)
        response.headers["Allow"] = allowed
        return response

    def error(self, message: str, status: int = 400) -> Response:
        return _page("Could not check", f"""<h1>Could not check this</h1>
<div class="card err"><p>{_esc(message)}</p></div>
<p><a href="/">Back</a></p>""", status)

    # Pages

    def landing(self) -> Response:
        return _page("cited: check every source", f"""<h1>Check every source in a draft</h1>
<p class="sub">Paste Markdown, or link to a raw Markdown file. cited re-fetches every page the draft cites.</p>
<form class="card" method="post" action="/check">
  <label for="markdown">Markdown</label>
  <textarea id="markdown" name="markdown" placeholder="Revenue grew 40% in 2025, [per the annual report](https://example.com/report)."></textarea>
  <label for="url">Or a link to a Markdown file</label>
  <input id="url" name="url" type="url" placeholder="https://raw.githubusercontent.com/you/repo/main/post.md">
  <div class="modes">
    <label class="mode"><input type="radio" name="mode" value="links" checked> <b>Free link check</b>
      <span>Dead and redirected sources, first {FREE_LINK_CAP} links. Nothing is stored.</span></label>
    <label class="mode"><input type="radio" name="mode" value="certificate"> <b>Certificate, $9</b>
      <span>Every claim checked against its page, as Verified, Not on page or Broken source,
      on a page you can share.</span></label>
  </div>
  <label for="code">Access code (certificate only)</label>
  <input id="code" name="code" type="text" autocomplete="off">
  <p class="why">No code yet? <a href="{_esc(self.payment_link)}">Buy a certificate</a> and we will email you one.</p>
  <button type="submit">Check</button>
</form>
<p class="why">A certificate shows whether each claim's own words, numbers and names are on the page it cites.
It does not show the page is right.</p>""")

    def certificate(self, cert_id: str) -> Response:
        if not CERT_ID.fullmatch(cert_id):
            return _page("Not found", "<h1>No such certificate</h1>", 404)
        storage = self._storage()
        data = storage.get(f"certs/{cert_id}.html")
        if data is None:
            return _page("Not found", "<h1>No such certificate</h1>", 404)
        return Response(200, data, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "public, max-age=300", **SECURITY_HEADERS})

    # Checking

    def check(self, body: bytes, content_type: str) -> Response:
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            raise UserError("Send the form from the upload page.", 415)
        if len(body) > MAX_FORM_BYTES:
            raise UserError(f"That is more than {MAX_FORM_BYTES:,} bytes. Check a shorter draft.", 413)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()}
        name, markdown = self.load_document(form.get("markdown", ""), form.get("url", "").strip())
        if form.get("mode") == "certificate":
            return self.run_certificate(name, markdown, form.get("code", "").strip())
        return self.run_link_check(name, markdown)

    def load_document(self, markdown: str, url: str) -> tuple[str, str]:
        if markdown.strip() and url:
            raise UserError("Paste Markdown or give a URL, not both.")
        if markdown.strip():
            if len(markdown.encode("utf-8")) > MAX_DOC_BYTES:
                raise UserError(f"Drafts are limited to {MAX_DOC_BYTES:,} bytes.", 413)
            return "pasted draft", markdown
        if not url:
            raise UserError("Paste some Markdown, or give a link to a Markdown file.")
        result = fetch_page(url, self.timeout, replace(self.policy, max_bytes=MAX_DOC_BYTES), keep_body=True)
        if result.blocked:
            raise UserError("That URL is not on a public address, so it was not fetched.")
        if result.status is None or result.status >= 400:
            raise UserError(f"Could not fetch that URL ({result.error or f'HTTP {result.status}'}).")
        if result.truncated:
            raise UserError(f"That file is larger than {MAX_DOC_BYTES:,} bytes.", 413)
        if result.kind != "text":
            raise UserError("That URL is a web page, not a Markdown file. Paste the Markdown instead, "
                            "or link to the raw file (for example raw.githubusercontent.com).")
        name = urllib.parse.urlsplit(result.final_url or url).path.rsplit("/", 1)[-1] or url
        return name, decode_body(result.extra["body"], result.extra.get("charset"))

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
        chips = " · ".join(f"{LINK_STATUS[k]} <b>{n}</b>" for k, n in counts.items() if n) or "no links found"
        rows = "".join(f'<tr><td><span class="tag s-{key}">{_esc(LINK_STATUS[key])}</span></td>'
                       f"<td>{_href(url)}<div class=\"why\">{_esc(detail)}</div></td></tr>"
                       for url, key, detail in statuses)
        capped = (f'<div class="card"><b>{skipped} more link(s) were not checked.</b> The free check covers the first '
                  f"{FREE_LINK_CAP}. A certificate checks every link.</div>") if skipped > 0 else ""
        table = (f'<div class="scroll"><table><tr><th>Status</th><th>Link</th></tr>{rows}</table></div>'
                 if rows else '<p class="why">No http(s) links were found in this draft.</p>')
        return _page(f"Link check: {name}", f"""<h1>Link check</h1>
<p class="sub"><code>{_esc(name)}</code> · {len(checked)} link(s) checked · {_esc(utc_now())}</p>
<div class="card">{chips}</div>
{capped}
{table}
<h2>Want every claim checked?</h2>
<div class="card">This only shows whether each source still loads. A certificate also checks whether each claim's
own words, numbers and names are on the page it cites, and gives you a page to share.
<a href="{_esc(self.payment_link)}">Get a certificate for $9</a>.</div>
<p><a href="/">Check another draft</a></p>""")

    def _storage(self) -> Storage:
        if self.storage is None:
            raise UserError(f"Certificates are unavailable: {self.storage_error or 'no storage configured'}.", 503)
        return self.storage

    def _code_ok(self, code: str) -> bool:
        # Compare against every code so timing does not reveal which one matched.
        matched = False
        for candidate in self.access_codes:
            matched |= hmac.compare_digest(candidate.encode(), code.encode())
        return matched

    def run_certificate(self, name: str, markdown: str, code: str) -> Response:
        if not self.access_codes:
            raise UserError("Certificates are not on sale yet.", 503)
        if not code or not self._code_ok(code):
            return _page("Access code needed", f"""<h1>A certificate needs an access code</h1>
<div class="card">{'That code is not valid. ' if code else ''}Certificates are $9 each.
<a href="{_esc(self.payment_link)}">Buy one</a> and we will email you a code. The free link check needs no code.</div>
<p><a href="/">Back</a></p>""", 402)
        storage = self._storage()

        pairs = extract(markdown)
        entries = [{"id": p.id, "claim": p.claim, "source_url": p.source_url, "line": p.line, "bucket": p.bucket}
                   for p in pairs if p.bucket != "excluded"]
        excluded = [{"id": p.id, "line": p.line, "link_text": p.link_text, "source_url": p.source_url,
                     "reason": p.reason} for p in pairs if p.bucket == "excluded"]
        if not entries:
            raise UserError("No checkable claims were found. A claim is a sentence with a link to its source.")
        if len(entries) > CERT_CLAIM_CAP:
            raise UserError(f"This draft has {len(entries)} checkable claims; a certificate covers up to {CERT_CLAIM_CAP}.")

        # Single use: the first request to create this marker wins. Redeemed
        # only after the input is known to be checkable.
        cert_id = secrets.token_urlsafe(12)
        marker = f"codes/{hashlib.sha256(code.encode()).hexdigest()}"
        if not storage.create(marker, cert_id.encode()):
            raise UserError("That access code has already been used.", 409)

        fetcher = Fetcher(policy=self.policy, timeout=self.timeout, retries=1,
                          per_host_delay=self.per_host_delay, use_wayback=self.use_wayback)
        rows, counts, _ = check_entries(entries, fetcher, concurrency=CONCURRENCY, draft=True)
        checked_at = utc_now()
        page = render_certificate(name, rows, checked_at, excluded=excluded, links_total=len(pairs),
                                  version=VERSION, footer_link=CITED_HOME)
        report = {"cited_version": VERSION, "checked_at": checked_at, "document": name,
                  "counts": counts, "results": rows, "excluded": excluded}
        storage.put(f"certs/{cert_id}.json", json.dumps(report, ensure_ascii=False).encode("utf-8"),
                    "application/json")
        storage.put(f"certs/{cert_id}.html", page.encode("utf-8"), "text/html; charset=utf-8")
        return Response(303, b"", {"Location": f"/c/{cert_id}"})


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
            response = get_app().handle(method, self.path, body, self.headers.get("Content-Type", ""))
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
