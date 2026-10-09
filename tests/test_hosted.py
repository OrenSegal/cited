"""The hosted app: the free link check is capped and never reaches a private
address, a certificate needs a single-use access code and is served from a
stable /c/{id} URL, and storage keys cannot escape their directory. All
network traffic goes to the local fixture server."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

import htmldraft
import storage as st
import webapp
from certificate import render
from conftest import fake_dns
from extract import extract
from safe_fetch import FetchPolicy

FILLER = " ".join(["The annual report also covers staffing, offices and product plans."] * 6)
CLAIM = "Example Corporation reported revenue of 12 million dollars in fiscal 2025."
PAGE = f"<html><body><p>{FILLER}</p><p>{CLAIM}</p><p>{FILLER}</p></body></html>"
OTHER = f"<html><body><p>{FILLER}</p><p>Nothing here about money at all.</p></body></html>"
FORM = "application/x-www-form-urlencoded"
LOCAL = FetchPolicy(allow_private=True)  # tests only: lets the app reach the fixture server


def _app(tmp_path, policy: FetchPolicy | None = None, codes=("good-code",)) -> webapp.App:
    return webapp.App(storage=st.LocalStorage(tmp_path / "data"), policy=policy, access_codes=frozenset(codes),
                      payment_link="https://buy.example/cert", timeout=5.0, per_host_delay=0, use_wayback=False)


def _post(app: webapp.App, **fields: str) -> webapp.Response:
    return app.handle("POST", "/check", urllib.parse.urlencode(fields).encode(), FORM)


def _text(response: webapp.Response) -> str:
    return response.body.decode("utf-8")


# SSRF: the public fetcher never contacts a non-public address


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "169.254.169.254", "[::1]", "2130706433"])
def test_free_check_never_contacts_private_addresses(tmp_path, server, host):
    server.add("/x", body=PAGE)
    draft = f"Revenue grew a great deal last year, [per the filing](http://{host}:{server.port}/x)."

    response = _post(_app(tmp_path), markdown=draft, mode="links")

    assert response.status == 200
    assert "Refused" in _text(response)
    assert server.total_hits == 0


def test_hostname_resolving_to_loopback_is_refused(tmp_path, server):
    server.add("/x", body=PAGE)
    draft = f"Revenue grew a great deal last year, [per the filing]({server.url('/x', host='innocent.example')})."
    with fake_dns({"innocent.example": "127.0.0.1"}):
        response = _post(_app(tmp_path), markdown=draft, mode="links")
    assert "Refused" in _text(response)
    assert server.total_hits == 0


def test_redirect_to_private_address_is_not_followed(tmp_path, server):
    server.add("/hop", status=302, headers={"Location": "http://127.0.0.1:{port}/secret"})
    server.add("/secret", body="internal")
    policy = FetchPolicy(allow_hosts=frozenset({"public.example"}))
    draft = f"Revenue grew a great deal last year, [per the filing]({server.url('/hop', host='public.example')})."
    with fake_dns({"public.example": "127.0.0.1"}):
        response = _post(_app(tmp_path, policy), markdown=draft, mode="links")
    assert "Refused" in _text(response)
    assert server.hits["/hop"] == 1
    assert server.hits["/secret"] == 0


def test_draft_url_on_a_private_address_is_never_fetched(tmp_path, server):
    server.add("/post.md", body="# Post", headers={"Content-Type": "text/markdown"})
    response = _post(_app(tmp_path), url=server.url("/post.md"), mode="links")
    assert response.status == 400
    assert "not on a public address" in _text(response)
    assert server.total_hits == 0


def test_oversized_form_is_rejected_before_parsing(tmp_path):
    body = b"markdown=" + b"a" * (webapp.MAX_FORM_BYTES + 1)
    response = _app(tmp_path).handle("POST", "/check", body, FORM)
    assert response.status == 413


# Free link check


def test_free_check_is_capped(tmp_path, server):
    for i in range(30):
        server.add(f"/p{i}", body=PAGE)
    draft = "\n\n".join(f"Claim number {i} says revenue rose sharply, [source]({server.url(f'/p{i}')})."
                        for i in range(30))

    response = _post(_app(tmp_path, LOCAL), markdown=draft, mode="links")

    assert response.status == 200
    assert server.total_hits == webapp.FREE_LINK_CAP == 25
    assert "5 more links were not checked" in _text(response)


def test_free_check_reports_dead_redirected_and_ok(tmp_path, server):
    server.add("/ok", body=PAGE)
    server.add("/moved", status=301, headers={"Location": "/ok"})
    server.add("/walled", status=403, body="no")
    draft = (f"First claim about revenue growth here, [a]({server.url('/ok')}).\n\n"
             f"Second claim about revenue growth here, [b]({server.url('/moved')}).\n\n"
             f"Third claim about revenue growth here, [c]({server.url('/gone')}).\n\n"
             f"Fourth claim about revenue growth here, [d]({server.url('/walled')}).\n")

    page = _text(_post(_app(tmp_path, LOCAL), markdown=draft, mode="links"))

    assert "Dead <b>1</b>" in page and "HTTP 404" in page
    assert "Redirected <b>1</b>" in page and f"Now at {server.url('/ok')}" in page
    assert "Blocks checkers <b>1</b>" in page
    assert "OK <b>1</b>" in page
    assert not list((tmp_path / "data").glob("**/*")), "the free check stores nothing"


def test_draft_can_come_from_a_markdown_url(tmp_path, server):
    server.add("/ok", body=PAGE)
    server.add("/post.md", body=f"A claim about revenue growth here, [a]({server.url('/ok')}).\n",
               headers={"Content-Type": "text/markdown; charset=utf-8"})
    page = _text(_post(_app(tmp_path, LOCAL), url=server.url("/post.md"), mode="links"))
    assert "<code>post.md</code>" in page
    assert "OK <b>1</b>" in page


def test_web_page_url_is_read_as_a_draft(tmp_path, server):
    server.add("/ok", body=PAGE)
    server.add("/post", body="""<html><head><title>My post</title></head><body>
<nav><a href="/nav-only">Home</a></nav>
<article><p>Example Corporation reported revenue of 12 million dollars in fiscal 2025,
<a href="/ok">per the filing</a>.</p></article></body></html>""")
    page = _text(_post(_app(tmp_path, LOCAL), url=server.url("/post"), mode="links"))
    assert "<code>My post</code>" in page
    assert "OK <b>1</b>" in page
    assert server.hits["/nav-only"] == 0


def test_web_page_links_to_private_addresses_are_refused(tmp_path, server):
    """SSRF on the HTML path: the page itself is allowed (allow_hosts stands in
    for a public host), the private targets it links to are never contacted."""
    server.add("/ok", body=PAGE)
    server.add("/secret", body="internal")
    server.add("/post", body=f"""<html><body><p>Revenue grew a great deal last year at Example Corporation,
<a href="/ok">per the filing</a>. Example Corporation also opened nine offices,
<a href="http://127.0.0.1:{server.port}/secret">per its blog</a>. And the metadata service says hello,
<a href="http://169.254.169.254/latest/meta-data/">per the cloud</a>.</p></body></html>""")
    policy = FetchPolicy(allow_hosts=frozenset({"public.example"}))
    with fake_dns({"public.example": "127.0.0.1"}):
        page = _text(_post(_app(tmp_path, policy), url=server.url("/post", host="public.example"), mode="links"))
    assert "Refused <b>2</b>" in page and "OK <b>1</b>" in page
    assert server.hits["/secret"] == 0


def test_pdf_url_is_refused_with_a_way_forward(tmp_path, server):
    server.add("/doc.pdf", body=b"%PDF-1.4 binary", headers={"Content-Type": "application/pdf"})
    response = _post(_app(tmp_path, LOCAL), url=server.url("/doc.pdf"), mode="links")
    assert response.status == 400
    assert "Paste the draft instead" in _text(response)


def test_html_draft_conversion():
    title, md = htmldraft.html_to_markdown(
        """<html><head><title> A  title </title><style>p{}</style></head><body>
<header><a href="/home">Home</a></header>
<h2>Results</h2><p>Revenue rose 40% in 2025, <a href="report.html">per the [annual] report</a>.</p>
<p hidden><a href="/hidden">hidden</a></p><p><a href="javascript:alert(1)">click</a></p>
<table><tr><td>Funding of $1B</td><td><a href="https://x.example/f">source</a></td></tr></table>
<h2>References</h2><ul><li><a href="https://x.example/r">Report</a></li></ul></body></html>""",
        "https://blog.example/posts/one")
    assert title == "A title"
    assert "[per the (annual) report](https://blog.example/posts/report.html)" in md
    assert "/home" not in md and "/hidden" not in md and "javascript" not in md
    assert "## References" in md
    kinds = {p.source_url: p.bucket for p in extract(md)}
    assert kinds["https://blog.example/posts/report.html"] == "checkable"
    assert kinds["https://x.example/r"] == "excluded"


# Paid certificate


def _cert_draft(server) -> str:
    server.add("/report", body=PAGE)
    server.add("/other", body=OTHER)
    return (f"# Post\n\nAccording to the filing, [{CLAIM}]({server.url('/report')})\n\n"
            f"Example Corporation raised a $40M Series B from Sequoia in 2024. [Source]({server.url('/other')})\n\n"
            f"Example Corporation also opened nine new offices in Europe. [Source]({server.url('/gone')})\n")


@pytest.mark.parametrize("code", ["", "wrong-code"])
def test_certificate_without_a_valid_code_points_to_payment(tmp_path, server, code):
    response = _post(_app(tmp_path, LOCAL), markdown=_cert_draft(server), mode="certificate", code=code)
    assert response.status == 402
    assert 'href="https://buy.example/cert"' in _text(response)
    assert server.total_hits == 0


def _start(app: webapp.App, draft: str, code: str = "good-code") -> tuple[str, str]:
    """POST a certificate; return (certificate id, delete key) from the owner page."""
    response = _post(app, markdown=draft, mode="certificate", code=code)
    assert response.status == 200, _text(response)
    page = _text(response)
    cert_id = re.search(r'href="/c/([A-Za-z0-9_-]+)"', page).group(1)
    key = re.search(r"/delete\?key=([A-Za-z0-9_-]+)", page).group(1)
    return cert_id, key


def _finish(app: webapp.App, cert_id: str, polls: int = 50) -> tuple[webapp.Response, int]:
    """Visit /c/{id} until the certificate is done; (final response, visits)."""
    for visit in range(1, polls + 1):
        response = app.handle("GET", f"/c/{cert_id}")
        if 'http-equiv="refresh"' not in _text(response):
            return response, visit
    raise AssertionError("certificate never finished")


def test_certificate_is_checked_stored_and_served_at_a_stable_url(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    cert_id, key = _start(app, _cert_draft(server))
    assert server.total_hits == 0, "the POST only starts the job"

    page, _ = _finish(app, cert_id)
    assert page.status == 200
    html = _text(page)
    for label in ("Verified", "Not on page", "Broken source"):
        assert label in html
    assert f'Checked by <a href="{webapp.CITED_HOME}">cited</a>' in html
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert key not in html

    report = json.loads((tmp_path / "data" / "certs" / f"{cert_id}.json").read_text())
    assert report["counts"]["verified"] == 1
    assert report["counts"]["unsupported"] == 1
    assert report["counts"]["broken"] == 1
    assert report["checked_at"] in html
    assert app.handle("GET", f"/c/{cert_id}").body == page.body


def test_access_code_is_single_use(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    draft = _cert_draft(server)
    _start(app, draft)
    again = _post(app, markdown=draft, mode="certificate", code="good-code")
    assert again.status == 409
    assert "already been used" in _text(again)


def test_code_is_not_spent_on_a_draft_with_nothing_to_check(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    assert _post(app, markdown="No links here.", mode="certificate", code="good-code").status == 400
    _start(app, _cert_draft(server))


# Jobs: many slow sources never make one request run long


def _slow(delay: float):
    def respond(handler):
        time.sleep(delay)
        body = PAGE.encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    return respond


def test_sixty_four_slow_sources_finish_over_bounded_visits(tmp_path, server):
    for i in range(64):
        server.add(f"/slow{i}", body=_slow(0.3))
    draft = "\n\n".join(f"According to the filing, [{CLAIM}]({server.url(f'/slow{i}')})" for i in range(64))
    # Scaled down from production (50 s budget, ~41 s worst batch): a 0.6 s
    # budget, so a visit runs one or two 0.3 s batches and then hands back.
    app = webapp.App(storage=st.LocalStorage(tmp_path / "data"), policy=LOCAL, access_codes=frozenset({"c"}),
                     timeout=5.0, job_timeout=5.0, per_host_delay=0, use_wayback=False,
                     poll_budget=0.6, worst_batch=0.4)
    cert_id, _ = _start(app, draft, "c")

    walls, visits = [], 0
    while True:
        visits += 1
        began = time.monotonic()
        response = app.handle("GET", f"/c/{cert_id}")
        walls.append(time.monotonic() - began)
        assert response.status == 200
        if 'http-equiv="refresh"' not in _text(response):
            break
        assert "of 64 checked" in _text(response)
        assert visits < 20
    assert max(walls) < 0.6 + 0.4 + 0.5, walls  # budget + one worst batch + slack
    assert visits >= 4, "64 claims in batches of 8 cannot finish in one or two visits"
    assert server.total_hits == 64
    report = json.loads((tmp_path / "data" / "certs" / f"{cert_id}.json").read_text())
    assert report["counts"]["verified"] == 64
    assert [r["index"] for r in report["results"]] == list(range(64))


def test_a_batch_leased_by_another_visitor_is_not_fetched_twice(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    cert_id, _ = _start(app, _cert_draft(server))
    slot = int(time.time() // webapp.LEASE_SECONDS)
    app.storage.create(f"jobs/{cert_id}/lease-b0-{slot}", b"")
    response = app.handle("GET", f"/c/{cert_id}")
    assert 'http-equiv="refresh"' in _text(response)
    assert server.total_hits == 0


def test_job_batches_never_contact_private_addresses(tmp_path, server):
    server.add("/report", body=PAGE)
    server.add("/secret", body=PAGE)
    draft = (f"First: [{CLAIM}]({server.url('/report', host='public.example')})\n\n"
             f"Second: [{CLAIM}](http://127.0.0.1:{server.port}/secret)\n\n"
             f"Third: [{CLAIM}](http://169.254.169.254/latest/meta-data/)\n")
    app = _app(tmp_path, FetchPolicy(allow_hosts=frozenset({"public.example"})))
    with fake_dns({"public.example": "127.0.0.1"}):
        cert_id, _ = _start(app, draft)
        _finish(app, cert_id)
    report = json.loads((tmp_path / "data" / "certs" / f"{cert_id}.json").read_text())
    assert [r["tier"] for r in report["results"]] == ["verified", "broken", "broken"]
    assert all("refused" in r["note"] for r in report["results"][1:])
    assert server.hits["/secret"] == 0


# Owner delete link


def test_owner_can_delete_with_the_secret_link_only(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    cert_id, key = _start(app, _cert_draft(server))
    _finish(app, cert_id)

    assert app.handle("GET", f"/c/{cert_id}/delete?key=wrong").status == 403
    assert app.handle("POST", f"/c/{cert_id}/delete", b"key=wrong", FORM).status == 403
    assert app.handle("GET", f"/c/{cert_id}").status == 200

    confirm = app.handle("GET", f"/c/{cert_id}/delete?key={key}")
    assert confirm.status == 200 and 'method="post"' in _text(confirm)
    assert app.handle("GET", f"/c/{cert_id}").status == 200, "GET alone deletes nothing"

    done = app.handle("POST", f"/c/{cert_id}/delete", f"key={key}".encode(), FORM)
    assert done.status == 200
    assert app.handle("GET", f"/c/{cert_id}").status == 410
    left = sorted(p.name for p in (tmp_path / "data").rglob("*") if p.is_file() and cert_id in str(p))
    assert left == [f"{cert_id}.deleted"] or all(n.startswith("lease-") or n.endswith(".deleted") for n in left), left
    assert app.handle("POST", f"/c/{cert_id}/delete", f"key={key}".encode(), FORM).status == 410


def test_delete_during_a_running_job_stops_it(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    cert_id, key = _start(app, _cert_draft(server))
    app.handle("POST", f"/c/{cert_id}/delete", f"key={key}".encode(), FORM)
    assert app.handle("GET", f"/c/{cert_id}").status == 410
    assert server.total_hits == 0


# Rate limit


def test_check_is_rate_limited_per_client(tmp_path):
    now = [1_000_000.0]
    app = webapp.App(storage=st.LocalStorage(tmp_path / "data"), rate_limit=(2, 600), clock=lambda: now[0])
    post = lambda ip: app.handle("POST", "/check", b"markdown=No+links.&mode=links", FORM, client_ip=ip)  # noqa: E731
    assert post("1.1.1.1").status == 200
    assert post("1.1.1.1").status == 200
    limited = post("1.1.1.1")
    assert limited.status == 429 and limited.headers["Retry-After"] == "600"
    assert post("2.2.2.2").status == 200
    now[0] += 600
    assert post("1.1.1.1").status == 200
    assert not any("1.1.1.1" in str(p) for p in (tmp_path / "data").rglob("*")), "addresses are stored hashed"


def test_rate_limit_is_shared_through_storage(tmp_path):
    store = st.LocalStorage(tmp_path / "data")
    a = webapp.RateLimiter(store, 3, 600, clock=lambda: 5.0)
    b = webapp.RateLimiter(store, 3, 600, clock=lambda: 5.0)  # a second function instance
    assert [a.allow("ip"), b.allow("ip"), a.allow("ip"), b.allow("ip")] == [True, True, True, False]


def test_rate_limit_without_storage_counts_in_memory():
    limiter = webapp.RateLimiter(None, 1, 600, clock=lambda: 5.0)
    assert limiter.allow("ip") and not limiter.allow("ip") and limiter.allow("other")


# Failures become pages, never dropped connections


class _BrokenStorage(st.LocalStorage):
    def get(self, key):
        raise st.StorageError("down")


def test_storage_outage_is_a_503_page(tmp_path):
    app = webapp.App(storage=_BrokenStorage(tmp_path), access_codes=frozenset({"c"}))
    response = app.handle("GET", "/c/abcdefghijklmnop")
    assert response.status == 503 and "try again" in _text(response)


def test_unexpected_error_is_a_500_page(tmp_path, monkeypatch, capsys):
    app = _app(tmp_path)
    monkeypatch.setattr(app, "landing", lambda: 1 / 0)
    response = app.handle("GET", "/")
    assert response.status == 500 and "<h1>" in _text(response)
    assert "ZeroDivisionError" in capsys.readouterr().err


# Stripe webhook (UNVERIFIED against live Stripe: fixture payload only)


def _stripe_event(session_id: str = "cs_test_a1B2c3") -> bytes:
    return json.dumps({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
        "id": session_id, "object": "checkout.session", "payment_status": "paid",
        "customer_details": {"email": "buyer@example.com"}}}}).encode()


def _sign(payload: bytes, secret: str, stamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def test_stripe_webhook_mints_one_code_per_paid_session(tmp_path, server):
    now = 1_700_000_000
    app = webapp.App(storage=st.LocalStorage(tmp_path / "data"), policy=LOCAL, stripe_secret="whsec_test",
                     clock=lambda: now, timeout=5.0, per_host_delay=0, use_wayback=False)
    payload = _stripe_event()
    hook = lambda body, sig: app.handle("POST", "/stripe/webhook", body, "application/json",  # noqa: E731
                                        headers={"stripe-signature": sig})

    assert hook(payload, _sign(payload, "wrong", now)).status == 400
    assert hook(payload, _sign(payload, "whsec_test", now - 3600)).status == 400
    assert hook(payload + b" ", _sign(payload, "whsec_test", now)).status == 400
    ok = hook(payload, _sign(payload, "whsec_test", now))
    assert ok.status == 200 and b"minted" in ok.body
    assert b"already handled" in hook(payload, _sign(payload, "whsec_test", now)).body

    order = json.loads((tmp_path / "data" / "orders" / "cs_test_a1B2c3.json").read_text())
    assert order["email"] == "buyer@example.com" and order["emailed"] is False
    _start(app, _cert_draft(server), order["code"])
    assert _post(app, markdown=_cert_draft(server), mode="certificate", code=order["code"]).status == 409


def test_stripe_webhook_is_off_without_a_secret(tmp_path):
    response = _app(tmp_path).handle("POST", "/stripe/webhook", _stripe_event(), "application/json")
    assert response.status == 503


def test_certificates_are_off_without_codes_or_storage(tmp_path, server):
    no_codes = _post(_app(tmp_path, LOCAL, codes=()), markdown=_cert_draft(server), mode="certificate", code="x")
    assert no_codes.status == 503
    no_store = webapp.App(storage=None, storage_error="not configured", access_codes=frozenset({"c"}))
    response = _post(no_store, markdown=_cert_draft(server), mode="certificate", code="c")
    assert response.status == 503
    assert server.total_hits == 0


@pytest.mark.parametrize("path", ["/c/missing-but-valid-id", "/c/..%2F..%2Fetc%2Fpasswd", "/c/../codes/x", "/c/a"])
def test_unknown_or_malformed_certificate_ids_are_404(tmp_path, path):
    assert _app(tmp_path).handle("GET", path).status == 404


def test_routes_and_methods(tmp_path):
    app = _app(tmp_path)
    landing = app.handle("GET", "/")
    assert landing.status == 200 and "STRIPE" not in _text(landing)
    assert "https://buy.example/cert" in _text(landing)
    assert app.handle("GET", "/check").status == 405
    assert app.handle("POST", "/").status == 405
    assert app.handle("GET", "/nope").status == 404
    assert app.handle("POST", "/check", b"markdown=x", "text/plain").status == 415


def test_payment_link_placeholder_until_configured():
    app = webapp.App.from_env({"CITED_DATA_DIR": "/nonexistent"})
    assert webapp.PAYMENT_LINK_PLACEHOLDER in _text(app.handle("GET", "/"))
    assert app.access_codes == frozenset()


def test_certificate_footer_is_unchanged_without_a_link():
    rows = [{"tier": "verified", "claim": "c", "source_url": "https://e.example", "quoted": 1, "topical": 1}]
    assert "Generated by cited" in render("doc", rows, "now")
    assert "Checked by <a" not in render("doc", rows, "now")
    assert "Checked by <a" not in render("doc", rows, "now", footer_link="javascript:alert(1)")


# The real HTTP handler, as the local server and Vercel run it


def test_handler_serves_the_app_over_http(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "_app", _app(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/healthz")
        assert conn.getresponse().read() == b"ok\n"
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.putrequest("POST", "/check")
        conn.putheader("Content-Type", FORM)
        conn.putheader("Content-Length", str(webapp.MAX_FORM_BYTES + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413
    finally:
        httpd.shutdown()
        httpd.server_close()


# Storage


def test_local_storage_create_is_first_writer_wins(tmp_path):
    store = st.LocalStorage(tmp_path)
    assert store.create("codes/abc", b"1") is True
    assert store.create("codes/abc", b"2") is False
    assert store.get("codes/abc") == b"1"
    store.put("certs/x.html", b"<p>", "text/html")
    assert store.get("certs/x.html") == b"<p>"
    assert store.get("certs/none.html") is None


@pytest.mark.parametrize("key", ["../escape", "certs/../../x", "/etc/passwd", "certs//x", ""])
def test_storage_keys_cannot_escape(tmp_path, key):
    with pytest.raises(ValueError):
        st.LocalStorage(tmp_path).put(key, b"x", "text/plain")


def test_storage_from_env_selection(tmp_path):
    assert isinstance(st.storage_from_env({"CITED_DATA_DIR": str(tmp_path)}), st.LocalStorage)
    supa = st.storage_from_env({"SUPABASE_URL": "https://p.supabase.co", "SUPABASE_SERVICE_KEY": "k",
                                "SUPABASE_BUCKET": "certs"})
    assert isinstance(supa, st.SupabaseStorage)
    with pytest.raises(st.StorageError):
        st.storage_from_env({"SUPABASE_URL": "https://p.supabase.co"})
    with pytest.raises(st.StorageError, match="on Vercel"):
        st.storage_from_env({"VERCEL": "1"})


class _FakeOpener:
    """Stands in for urllib's opener: records requests, answers from a script."""

    def __init__(self, answers: list[int | bytes]) -> None:
        self.answers = answers
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, int) and answer >= 300:
            raise urllib.error.HTTPError(request.full_url, answer, "err", {}, io.BytesIO(b""))
        response = io.BytesIO(answer if isinstance(answer, bytes) else b"")
        response.status = 200 if isinstance(answer, bytes) else answer
        return response


def test_supabase_adapter_requests():
    opener = _FakeOpener([200, 409, b"<html>", 404])
    store = st.SupabaseStorage("https://p.supabase.co/", "service-key", "certs", opener=opener)

    store.put("certs/a.html", b"<html>", "text/html")
    assert store.create("codes/abc", b"id") is False
    assert store.get("certs/a.html") == b"<html>"
    assert store.get("certs/missing.html") is None

    put = opener.requests[0]
    assert put.full_url == "https://p.supabase.co/storage/v1/object/certs/certs/a.html"
    assert put.get_method() == "POST"
    assert put.get_header("Authorization") == "Bearer service-key"
    assert put.get_header("X-upsert") == "true"
    assert opener.requests[1].get_header("X-upsert") == "false"
    with pytest.raises(ValueError):
        store.get("../other-bucket/x")


def test_supabase_delete_request_and_missing_object():
    opener = _FakeOpener([200, 404, 500])
    store = st.SupabaseStorage("https://p.supabase.co", "service-key", "certs", opener=opener)
    store.delete("certs/a.html")
    store.delete("certs/gone.html")
    with pytest.raises(st.StorageError):
        store.delete("certs/b.html")
    assert opener.requests[0].get_method() == "DELETE"
    assert opener.requests[0].full_url == "https://p.supabase.co/storage/v1/object/certs/certs/a.html"


def test_local_storage_delete(tmp_path):
    store = st.LocalStorage(tmp_path)
    store.put("certs/x.html", b"1", "text/html")
    store.delete("certs/x.html")
    store.delete("certs/x.html")
    assert store.get("certs/x.html") is None


@pytest.mark.parametrize("on_vercel", [False, True])
def test_handler_trusts_forwarded_address_only_on_vercel(tmp_path, monkeypatch, on_vercel):
    monkeypatch.setattr(webapp, "_app", webapp.App(storage=None, rate_limit=(1, 600)))
    if on_vercel:
        monkeypatch.setenv("VERCEL", "1")
    else:
        monkeypatch.delenv("VERCEL", raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        statuses = []
        for forwarded in ("9.9.9.1", "9.9.9.2"):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("POST", "/check", "markdown=No+links.&mode=links",
                         {"Content-Type": FORM, "X-Forwarded-For": forwarded})
            statuses.append(conn.getresponse().status)
    finally:
        httpd.shutdown()
        httpd.server_close()
    # Off Vercel the header is client-controlled, so both requests count against 127.0.0.1.
    assert statuses == ([200, 200] if on_vercel else [200, 429])


class _JobWriteFails(st.LocalStorage):
    def put(self, key, data, content_type="application/octet-stream"):
        if key.startswith("jobs/"):
            raise st.StorageError("down")
        return super().put(key, data, content_type)


def test_a_failed_job_write_leaves_the_code_unspent(tmp_path, server):
    data = tmp_path / "data"
    flaky = webapp.App(storage=_JobWriteFails(data), policy=LOCAL, access_codes=frozenset({"good-code"}),
                       timeout=5.0, per_host_delay=0, use_wayback=False)
    assert _post(flaky, markdown=_cert_draft(server), mode="certificate", code="good-code").status == 503
    _start(_app(tmp_path, LOCAL), _cert_draft(server))  # same storage dir: the code still works


def test_stripe_webhook_needs_an_explicit_paid_status(tmp_path):
    now = 1_700_000_000
    app = webapp.App(storage=st.LocalStorage(tmp_path / "data"), stripe_secret="whsec_test", clock=lambda: now)
    event = json.loads(_stripe_event())
    del event["data"]["object"]["payment_status"]
    payload = json.dumps(event).encode()
    response = app.handle("POST", "/stripe/webhook", payload, "application/json",
                          headers={"stripe-signature": _sign(payload, "whsec_test", now)})
    assert b"minted" not in response.body
    assert not (tmp_path / "data" / "orders").exists()


class _ClaimFails(st.LocalStorage):
    def create(self, key, data):
        if key.startswith("codes/"):
            raise st.StorageError("down")
        return super().create(key, data)


def test_a_failed_code_claim_leaves_no_orphan_job(tmp_path, server):
    data = tmp_path / "data"
    flaky = webapp.App(storage=_ClaimFails(data), policy=LOCAL, access_codes=frozenset({"good-code"}),
                       timeout=5.0, per_host_delay=0, use_wayback=False)
    assert _post(flaky, markdown=_cert_draft(server), mode="certificate", code="good-code").status == 503
    assert not list(data.glob("jobs/*.json"))


def test_a_used_code_leaves_no_orphan_job(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    _start(app, _cert_draft(server))
    jobs = set((tmp_path / "data" / "jobs").glob("*.json"))
    assert _post(app, markdown=_cert_draft(server), mode="certificate", code="good-code").status == 409
    assert set((tmp_path / "data" / "jobs").glob("*.json")) == jobs
