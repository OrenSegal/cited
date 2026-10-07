"""Markdown drafts through the CLI: the pairs the extractor finds are checked
like a claims file, excluded links are never fetched and never fail a run,
and the certificate escapes everything it did not write itself. All network
traffic goes to the local fixture server."""

from __future__ import annotations

import json

import verify_claims as vc
from certificate import render

FILLER = " ".join(["The annual report also covers staffing, offices and product plans."] * 6)
CLAIM = "Example Corporation reported revenue of 12 million dollars in fiscal 2025."
PAGE = f"<html><body><p>{FILLER}</p><p>{CLAIM}</p><p>{FILLER}</p></body></html>"
OTHER = f"<html><body><p>{FILLER}</p><p>Nothing here about money at all.</p></body></html>"
LOCAL = ["--allow-private-addresses", "--per-host-delay", "0", "--retries", "0", "--no-wayback"]


def _draft(tmp_path, server, body: str) -> str:
    path = tmp_path / "post.md"
    path.write_text(body.replace("{base}", server.url("")), encoding="utf-8")
    return str(path)


def test_draft_claims_are_checked_and_excluded_links_are_not_fetched(tmp_path, server, capsys):
    server.add("/report", body=PAGE)
    server.add("/other", body=OTHER)
    server.add("/blog", body=PAGE)
    draft = _draft(tmp_path, server, (
        f"# Post\n\nAccording to the filing, [{CLAIM}]({{base}}/report)\n\n"
        "Example Corporation raised a $40M Series B from Sequoia in 2024. [Source]({base}/other)\n\n"
        "You can read more [here]({base}/blog) if you want.\n"))

    code = vc.main([draft, "--json", *LOCAL])
    report = json.loads(capsys.readouterr().out)

    tiers = {r["bucket"]: r["tier"] for r in report["results"]}
    assert tiers == {"checkable": "verified", "repaired": "unsupported"}
    assert all(isinstance(r["line"], int) for r in report["results"])
    assert report["draft"]["links"] == 3
    assert [x["link_text"] for x in report["draft"]["excluded"]] == ["here"]
    assert server.hits["/blog"] == 0
    assert code == vc.EXIT_BLOCKING


def test_a_draft_whose_only_links_are_excluded_passes(tmp_path, server, capsys):
    draft = _draft(tmp_path, server, "Read more [here]({base}/x).\n\n## Sources\n\n- [Annual report]({base}/a)\n")
    assert vc.main([draft, *LOCAL]) == vc.EXIT_OK
    assert server.total_hits == 0
    assert "2 link(s) in the draft were not checked" in capsys.readouterr().out


def test_extract_only_never_touches_the_network(tmp_path, server, capsys):
    draft = _draft(tmp_path, server, "Revenue grew 38% year over year. [Source]({base}/ir)\n")
    assert vc.main([draft, "--extract-only"]) == vc.EXIT_OK
    out = capsys.readouterr().out
    assert "REPAIR" in out and "Revenue grew 38%" in out
    assert server.total_hits == 0


def test_extract_only_refuses_a_claims_file(tmp_path, capsys):
    path = tmp_path / "claims.json"
    path.write_text("[]", encoding="utf-8")
    try:
        vc.main([str(path), "--extract-only"])
    except SystemExit as exc:
        assert exc.code == vc.EXIT_USAGE
    else:
        raise AssertionError("expected a usage error")


def test_missing_draft_is_a_usage_error(tmp_path, capsys):
    assert vc.main([str(tmp_path / "nope.md")]) == vc.EXIT_USAGE
    assert "file not found" in capsys.readouterr().err


def test_certificate_is_written_for_a_draft(tmp_path, server, capsys):
    server.add("/report", body=PAGE)
    draft = _draft(tmp_path, server, f"The filing says [{CLAIM}]({{base}}/report)\n\nSee [the docs]({{base}}/d).\n")
    cert = tmp_path / "out" / "cert.html"
    assert vc.main([draft, "--certificate", str(cert), *LOCAL]) == vc.EXIT_OK
    page = cert.read_text(encoding="utf-8")
    assert "Every claim was quoted from its cited source" in page
    assert "Links deliberately not checked (1)" in page
    assert "Certificate written" in capsys.readouterr().out


def test_certificate_works_for_a_claims_file_too(tmp_path, server, capsys):
    server.add("/other", body=OTHER)
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([{"id": "a", "claim": CLAIM, "source_url": server.url("/other")}]), encoding="utf-8")
    cert = tmp_path / "cert.html"
    assert vc.main([str(claims), "--certificate", str(cert), *LOCAL]) == vc.EXIT_BLOCKING
    assert "did not check out" in cert.read_text(encoding="utf-8")


def test_certificate_escapes_input_and_page_text():
    row = {"tier": "unsupported", "claim": "<script>alert(1)</script>", "source_url": 'https://a.test/"><x',
           "note": "<img src=x onerror=alert(1)>", "quoted": 0.0, "topical": 0.0, "checked_against": "live"}
    excluded = [{"line": 3, "link_text": "<b>here</b>", "source_url": "https://a.test", "reason": "<i>why</i>"}]
    page = render("<doc>.md", [row], "2026-10-06T00:00:00Z", excluded=excluded, links_total=2)
    for raw in ("<script>alert", "<img src=x", '"><x', "<b>here", "<i>why", "<doc>"):
        assert raw not in page, raw
    assert "&lt;script&gt;" in page


def test_certificate_links_only_http_sources():
    row = {"tier": "broken", "claim": "c", "source_url": " JavaScript:alert(1)", "quoted": 0.0, "topical": 0.0}
    page = render("d", [row], "t")
    assert 'href="' not in page.split("<body>", 1)[1].split('class="claim"', 1)[1]
    assert "<code>JavaScript:alert(1)</code>" in page
    ok = render("d", [dict(row, source_url="https://a.test/x")], "t")
    assert '<a href="https://a.test/x">' in ok


def test_certificate_verdict_follows_cited_blocking_rules():
    review = {"tier": "low_match", "claim": "c", "source_url": "https://a.test", "quoted": 0.1, "topical": 0.5}
    assert "need a person to check" in render("d", [review], "t")
    unchecked = dict(review, tier="unverified")
    assert "were never checked, nothing else blocks" in render("d", [unchecked], "t")
    assert "1 claim(s) were never checked<" in render("d", [unchecked], "t", strict=True)
