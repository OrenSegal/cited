"""End to end: real HTTP against a local server that serves the pages in
tests/fixtures/site, through main() and through the bin/cited wrapper.
Covers every tier and the Wayback fallback. No real network."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import verify_claims as vc
from conftest import FIXTURES, fake_dns

ROOT = FIXTURES.parent.parent
BIN = ROOT / "bin" / "cited"
SITE = FIXTURES / "site"
LOCAL = ["--allow-private-addresses", "--per-host-delay", "0", "--retries", "0"]


def _serve_site(server):
    for page in SITE.iterdir():
        server.add(f"/{page.name}", body=page.read_bytes())
    server.add("/annual-report.pdf", body=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n",
               headers={"Content-Type": "application/pdf"})
    server.add("/rate-limited.html", 429, body="slow down", headers={"Content-Type": "text/plain"})
    # /deleted-page.html is not registered: the server answers 404.


def _claims(tmp_path, server):
    raw = (FIXTURES / "claims.json").read_text(encoding="utf-8").replace("{base}", server.url("").rstrip("/"))
    claims = json.loads(raw)
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(claims), encoding="utf-8")
    return path, {c["id"]: c["expect"] for c in claims}


def test_every_tier_through_main(tmp_path, server, capsys):
    _serve_site(server)
    path, expected = _claims(tmp_path, server)
    code = vc.main([str(path), "--json", "--no-wayback"] + LOCAL)
    report = json.loads(capsys.readouterr().out)
    got = {r["id"]: r["tier"] for r in report["results"]}
    assert got == expected
    assert code == vc.EXIT_BLOCKING
    assert set(got.values()) == {"verified", "low_match", "unsupported", "unverified", "broken", "snippet_only"}
    # Each distinct URL was fetched once even though four claims cite the article.
    assert server.hits["/article.html"] == 1
    by_id = {r["id"]: r for r in report["results"]}
    assert "48 billion" in by_id["unsupported-wrong-number"]["note"]
    assert "Sequoia" in by_id["unsupported-invented-investor"]["note"]  # only in a <script>, which is ignored
    assert by_id["unverified-pdf"]["note"].startswith("Source is a PDF")
    assert by_id["broken-404"]["http_status"] == 404


def test_every_tier_through_the_bin_wrapper(tmp_path, server):
    _serve_site(server)
    path, expected = _claims(tmp_path, server)
    env = {**os.environ, "CITED_PYTHON": sys.executable}
    proc = subprocess.run([str(BIN), str(path), "--json", "--no-wayback"] + LOCAL,
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 1, proc.stderr
    report = json.loads(proc.stdout)
    assert {r["id"]: r["tier"] for r in report["results"]} == expected
    assert report["summary"]["exit_code"] == 1


def test_bin_wrapper_reports_invalid_input_with_exit_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"claim": "x"}', encoding="utf-8")
    proc = subprocess.run([str(BIN), str(bad)], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "CITED_PYTHON": sys.executable})
    assert proc.returncode == 2
    assert "top level must be a JSON array" in proc.stderr


def test_bin_wrapper_works_through_a_symlink(tmp_path):
    link = tmp_path / "cited"
    link.symlink_to(BIN)
    proc = subprocess.run([str(link), "--version"], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "CITED_PYTHON": sys.executable})
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"cited {vc.VERSION}"


def test_default_policy_refuses_the_local_fixture_server(tmp_path, server, capsys):
    # Without --allow-private-addresses the same run never reaches 127.0.0.1.
    _serve_site(server)
    path, _ = _claims(tmp_path, server)
    code = vc.main([str(path), "--json", "--per-host-delay", "0", "--retries", "0"])
    report = json.loads(capsys.readouterr().out)
    assert server.total_hits == 0
    assert code == vc.EXIT_BLOCKING
    assert {r["tier"] for r in report["results"]} == {"broken"}
    assert all("not a public address" in r["note"] or "Invalid source_url" in r["note"]
               for r in report["results"])


@pytest.fixture
def local_wayback(server, monkeypatch):
    """Point the Wayback availability API at the fixture server. Snapshots
    on the API's own host are accepted, as they would be on archive.org."""
    monkeypatch.setattr(vc, "WAYBACK_API", server.url("/wayback/available?url="))
    return server


def _wayback_answer(server, snapshot_path):
    snapshot = server.url(snapshot_path) if snapshot_path else ""
    body = {"archived_snapshots": {"closest": {"available": True, "url": snapshot, "timestamp": "20240115093000"}}
            if snapshot else {}}
    server.add("/wayback/available", body=json.dumps(body), headers={"Content-Type": "application/json"})


def test_dead_page_is_checked_against_the_wayback_snapshot(tmp_path, local_wayback, capsys):
    server = local_wayback
    server.add("/web/20240115093000id_/press.html", body=(SITE / "archived.html").read_bytes())
    _wayback_answer(server, "/web/20240115093000/press.html")
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{
        "id": "archived",
        "claim": "Contoso Labs today announced that its research team published 12 peer-reviewed papers in 2024",
        "source_url": server.url("/press.html"),  # 404 live
    }]), encoding="utf-8")
    code = vc.main([str(path), "--json"] + LOCAL)
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert code == vc.EXIT_OK
    assert result["tier"] == "verified"
    assert result["checked_against"] == "wayback"
    assert result["snapshot_url"].endswith("/web/20240115093000/press.html")
    assert "Wayback archive (20240115)" in result["note"]
    assert server.hits["/press.html"] == 1 and server.hits["/wayback/available"] == 1


def test_thin_page_falls_back_to_wayback(tmp_path, local_wayback, capsys):
    server = local_wayback
    server.add("/app", body=(SITE / "thin.html").read_bytes())
    server.add("/web/1id_/app", body=(SITE / "archived.html").read_bytes())
    _wayback_answer(server, "/web/1/app")
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"claim": "Contoso Labs published 12 peer-reviewed papers in 2025.",
                                 "source_url": server.url("/app")}]), encoding="utf-8")
    code = vc.main([str(path), "--json"] + LOCAL)
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["tier"] == "unsupported"  # 2025 is not on the archived page
    assert "live page yielded no text" in result["note"]
    assert code == vc.EXIT_BLOCKING


def test_no_snapshot_leaves_the_source_broken(tmp_path, local_wayback, capsys):
    server = local_wayback
    _wayback_answer(server, None)
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"claim": "anything at all", "source_url": server.url("/gone")}]), encoding="utf-8")
    code = vc.main([str(path), "--json"] + LOCAL)
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert (code, result["tier"]) == (vc.EXIT_BLOCKING, "broken")
    assert "no archived copy found" in result["note"]


def test_bot_walled_host_is_snippet_only(tmp_path, server, capsys):
    server.add("/status/1", 403, body="forbidden", headers={"Content-Type": "text/plain"})
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"claim": "A post that says something.",
                                 "source_url": server.url("/status/1", host="x.com")}]), encoding="utf-8")
    with fake_dns({"x.com": "127.0.0.1"}):
        code = vc.main([str(path), "--json", "--no-wayback", "--allow-host", "x.com",
                        "--per-host-delay", "0", "--retries", "0"])
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["tier"] == "snippet_only"
    assert "x.com blocks automated fetch" in result["note"]
    assert code == vc.EXIT_OK


def test_archived_pdf_is_unverified_not_unsupported(tmp_path, local_wayback, capsys):
    # Regression: without id_, Wayback answers a PDF capture with a ~250-char
    # HTML toolbar page, which was checked as the source and the claim
    # reported absent (a false fabrication signal).
    server = local_wayback
    server.add("/web/20260110222031/doc.pdf", body="<p>" + "Wayback Machine captures toolbar " * 10 + "</p>")
    server.add("/web/20260110222031id_/doc.pdf", body=b"%PDF-1.4\n", headers={"Content-Type": "application/pdf"})
    _wayback_answer(server, "/web/20260110222031/doc.pdf")
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"claim": "Four domain names are reserved.",
                                 "source_url": server.url("/doc.pdf")}]), encoding="utf-8")
    code = vc.main([str(path), "--json"] + LOCAL)
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["tier"] == "unverified"
    assert "Wayback copy (20240115) is a PDF" in result["note"]
    assert server.hits["/web/20260110222031/doc.pdf"] == 0
    assert code == vc.EXIT_OK


def test_archived_error_page_is_not_used(tmp_path, local_wayback, capsys):
    server = local_wayback
    for snapshot in ("/web/2024/gone", "/web/2024id_/gone"):
        server.add(snapshot, body="<p>" + "Page not found on this site. " * 20 + "</p>")
    server.add("/wayback/available", body=json.dumps({"archived_snapshots": {"closest": {
        "available": True, "status": "404", "url": server.url("/web/2024/gone"), "timestamp": "2024"}}}),
        headers={"Content-Type": "application/json"})
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"claim": "Anything.", "source_url": server.url("/gone")}]), encoding="utf-8")
    vc.main([str(path), "--json"] + LOCAL)
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["tier"] == "broken"
    assert server.hits["/web/2024/gone"] == server.hits["/web/2024id_/gone"] == 0


@pytest.mark.parametrize("given, raw", [
    ("http://web.archive.org/web/20260110222031/https://a.test/x.pdf",
     "http://web.archive.org/web/20260110222031id_/https://a.test/x.pdf"),
    ("https://web.archive.org/web/2024im_/https://a.test/",
     "https://web.archive.org/web/2024id_/https://a.test/"),
    ("https://web.archive.org/web/2024id_/https://a.test/",
     "https://web.archive.org/web/2024id_/https://a.test/"),
    ("https://archive.org/details/thing", "https://archive.org/details/thing"),
])
def test_raw_snapshot_url(given, raw):
    assert vc.raw_snapshot_url(given) == raw
