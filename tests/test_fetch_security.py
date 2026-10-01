"""SSRF and hostile-content regressions for the fetcher.

Every "never contacted" assertion is made on the fixture server's hit
counter, not on the returned status: a refused fetch and a fetch that
reached an internal service and then failed look the same from the
return value alone.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

import pytest
from conftest import fake_dns

import verify_claims as vc


def _sf():
    import safe_fetch

    return safe_fetch


def _local_policy(**overrides):
    """Policy for fixture tests that need to reach 127.0.0.1 on purpose."""
    return _sf().FetchPolicy(allow_private=True, **overrides)


PAGE = "<html><body><p>" + "internal admin page " * 30 + "</p></body></html>"


# ── Default policy: private, loopback and odd spellings are never contacted ──

@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "localhost",
    "2130706433",          # decimal 127.0.0.1
    "0x7f000001",          # hex 127.0.0.1
    "0177.0.0.1",          # octal first octet
    "127.1",               # shorthand
    "[::ffff:127.0.0.1]",  # IPv4-mapped IPv6
])
def test_loopback_spellings_are_never_contacted(server, host):
    server.add("/", body=PAGE)
    status, text = vc.fetch_text(server.url("/", host=host), 3)
    assert server.total_hits == 0
    assert status is None and text == ""


def test_hostname_resolving_to_loopback_is_never_contacted(server):
    server.add("/", body=PAGE)
    with fake_dns({"evil.test": "127.0.0.1"}):
        status, text = vc.fetch_text(server.url("/", host="evil.test"), 3)
    assert server.total_hits == 0
    assert status is None and text == ""


def test_hostname_with_any_private_address_is_refused(server):
    # Round-robin trick: one public record, one private. Refuse the host.
    server.add("/", body=PAGE)
    with fake_dns({"mixed.test": ["93.184.215.14", "127.0.0.1"]}):
        result = _sf().fetch_page(server.url("/", host="mixed.test"), 3, _sf().FetchPolicy())
    assert server.total_hits == 0
    assert result.blocked
    assert "127.0.0.1" in result.error


def test_metadata_endpoint_is_refused_before_connecting():
    result = _sf().fetch_page("http://169.254.169.254/latest/meta-data/", 3, _sf().FetchPolicy())
    assert result.blocked and result.status is None


@pytest.mark.parametrize("ip", [
    "0.0.0.0", "10.0.0.1", "100.100.100.200", "127.0.0.1", "169.254.169.254",
    "172.16.0.1", "192.0.0.1", "192.168.1.1", "198.18.0.1", "224.0.0.1",
    "240.0.0.1", "255.255.255.255",
    "::", "::1", "fe80::1", "fc00::1", "fd00:ec2::254", "ff02::1",
    "::ffff:10.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "2002:a00:1::1",
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo, client 127.0.0.1
])
def test_non_public_addresses_are_blocked(ip):
    import ipaddress

    assert _sf().is_blocked_ip(ipaddress.ip_address(ip)), ip


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.215.14", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(ip):
    import ipaddress

    assert not _sf().is_blocked_ip(ipaddress.ip_address(ip)), ip


# ── Schemes ─────────────────────────────────────────────────────────────────

def test_file_scheme_is_never_read():
    fd, path = tempfile.mkstemp(suffix=".html")
    os.write(fd, PAGE.encode())
    os.close(fd)
    try:
        status, text = vc.fetch_text("file://" + path, 3)
    finally:
        os.unlink(path)
    assert text == ""
    assert status is None


def test_data_scheme_is_never_read():
    status, text = vc.fetch_text("data:text/html," + PAGE, 3)
    assert text == "" and status is None


@pytest.mark.parametrize("url", [
    "ftp://127.0.0.1/x", "gopher://127.0.0.1/x", "javascript:alert(1)",
    "file:///etc/passwd", "data:,x", "http:///no-host", "//example.com/x",
])
def test_url_problem_rejects_non_http_and_hostless(url):
    assert _sf().url_problem(url, _sf().FetchPolicy())


def test_url_problem_accepts_public_https():
    assert _sf().url_problem("https://example.com/a?b=c", _sf().FetchPolicy()) is None


# ── Redirects ───────────────────────────────────────────────────────────────

def test_redirect_from_allowed_host_to_private_host_is_not_followed(server):
    # public.test is allow-listed (stands in for a public site); it redirects
    # to internal.test, which resolves to a private address.
    server.add("/start", 302, headers={"Location": "http://internal.test:{port}/admin"})
    server.add("/admin", body=PAGE)
    policy = _sf().FetchPolicy(allow_hosts=frozenset({"public.test"}))
    with fake_dns({"public.test": "127.0.0.1", "internal.test": "127.0.0.1"}):
        result = _sf().fetch_page(server.url("/start", host="public.test"), 3, policy)
    assert server.hits["/start"] == 1
    assert server.hits["/admin"] == 0
    assert result.blocked and result.status is None


def test_redirect_to_ftp_is_not_followed(server):
    accepted = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    ftp_port = listener.getsockname()[1]

    def accept():
        try:
            conn, _ = listener.accept()
            accepted.append(conn)
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    server.add("/go", 302, headers={"Location": f"ftp://127.0.0.1:{ftp_port}/x"})
    try:
        _sf().fetch_page(server.url("/go"), 2, _local_policy())
        time.sleep(0.2)
    finally:
        listener.close()
        thread.join(timeout=4)
    assert accepted == []


def test_redirect_chain_is_capped(server):
    for i in range(20):
        server.add(f"/r{i}", 302, headers={"Location": f"/r{i + 1}"})
    result = _sf().fetch_page(server.url("/r0"), 3, _local_policy(max_redirects=3))
    assert server.total_hits == 4  # the first request plus three redirects
    assert result.status is None
    assert "redirect" in result.error.lower()


# ── Size, time, content ────────────────────────────────────────────────────

def test_response_size_is_capped_and_flagged(server):
    server.add("/big", body="<p>" + "filler words here " * 100_000 + "</p>")
    result = _sf().fetch_page(server.url("/big"), 5, _local_policy(max_bytes=50_000))
    assert result.status == 200
    assert result.truncated
    assert len(result.text) <= 50_000


def test_slow_drip_response_is_cut_off_by_the_total_timeout(server):
    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.end_headers()
        try:
            for _ in range(60):
                handler.wfile.write(b"<p>drip</p>")
                handler.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass

    server.add("/drip", body=drip)
    started = time.monotonic()
    vc.fetch_text(server.url("/drip"), 1, _local_policy())
    assert time.monotonic() - started < 4.0


def test_deeply_nested_json_ld_does_not_crash_extraction():
    html = '<script type="application/ld+json">' + "[" * 50_000 + "]" * 50_000 + "</script><p>ok</p>"
    parser = vc._TextExtractor()
    parser.feed(html)
    assert "ok" in parser.text()


def test_unknown_charset_falls_back_instead_of_failing(server):
    server.add("/cs", body="<p>" + "hello world " * 40 + "</p>",
               headers={"Content-Type": "text/html; charset=x-bogus-123"})
    status, text = vc.fetch_text(server.url("/cs"), 3, _local_policy())
    assert status == 200
    assert "hello world" in text


def test_script_content_is_never_part_of_the_text(server):
    server.add("/js", body="<script>document.write('INJECTED CLAIM TEXT')</script><p>" + "plain " * 50 + "</p>")
    _, text = vc.fetch_text(server.url("/js"), 3, _local_policy())
    assert "INJECTED" not in text


def test_wayback_snapshot_url_is_validated(server):
    # A hostile or spoofed availability response must not steer the fetcher
    # to a local file or an internal address.
    server.add("/wayback", body='{"archived_snapshots": {"closest": {"available": true, '
               '"url": "http://169.254.169.254/latest/", "timestamp": "20260101000000"}}}',
               headers={"Content-Type": "application/json"})
    policy = _sf().FetchPolicy(allow_hosts=frozenset({"archive.test"}))
    fetcher = vc.Fetcher(policy=policy, timeout=3, retries=0, per_host_delay=0)
    with fake_dns({"archive.test": "127.0.0.1"}):
        fetcher.wayback_api = f"http://archive.test:{server.port}/wayback?url="
        snapshot_url, _, page = fetcher.wayback("https://example.com/gone")
    assert page is None or not page.text
    assert server.hits["/wayback"] == 1
