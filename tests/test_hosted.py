"""The hosted app: the free link check is capped and never reaches a private
address, a certificate needs a single-use access code and is served from a
stable /c/{id} URL, and storage keys cannot escape their directory. All
network traffic goes to the local fixture server."""

from __future__ import annotations

import http.client
import io
import json
import threading
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

import storage as st
import webapp
from certificate import render
from conftest import fake_dns
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
    assert "5 more link(s) were not checked" in _text(response)


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


def test_html_url_is_refused_with_a_way_forward(tmp_path, server):
    server.add("/post", body=PAGE)
    response = _post(_app(tmp_path, LOCAL), url=server.url("/post"), mode="links")
    assert response.status == 400
    assert "raw file" in _text(response)


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


def test_certificate_is_stored_and_served_at_a_stable_url(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    response = _post(app, markdown=_cert_draft(server), mode="certificate", code="good-code")

    assert response.status == 303
    location = response.headers["Location"]
    assert location.startswith("/c/")
    page = app.handle("GET", location)
    assert page.status == 200
    html = _text(page)
    for label in ("Verified", "Not on page", "Broken source"):
        assert label in html
    assert f'Checked by <a href="{webapp.CITED_HOME}">cited</a>' in html
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]

    report = json.loads((tmp_path / "data" / "certs" / f"{location[3:]}.json").read_text())
    assert report["counts"]["verified"] == 1
    assert report["counts"]["unsupported"] == 1
    assert report["counts"]["broken"] == 1
    assert report["checked_at"] in html


def test_access_code_is_single_use(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    draft = _cert_draft(server)
    assert _post(app, markdown=draft, mode="certificate", code="good-code").status == 303
    again = _post(app, markdown=draft, mode="certificate", code="good-code")
    assert again.status == 409
    assert "already been used" in _text(again)


def test_code_is_not_spent_on_a_draft_with_nothing_to_check(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    assert _post(app, markdown="No links here.", mode="certificate", code="good-code").status == 400
    assert _post(app, markdown=_cert_draft(server), mode="certificate", code="good-code").status == 303


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
