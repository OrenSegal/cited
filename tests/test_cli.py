"""CLI contract: input validation, exit codes, --json schema, cache and
offline mode, retries, politeness, de-duplication and decoding. All
network traffic goes to the local fixture server."""

from __future__ import annotations

import io
import json

import pytest

import verify_claims as vc
from conftest import fake_dns
from safe_fetch import FetchPolicy, decode_body

FILLER = " ".join(["The annual report also covers staffing, offices and product plans."] * 6)
CLAIM = "Example Corporation reported revenue of 12 million dollars in fiscal 2025."
PAGE = f"<html><body><h1>Annual report</h1><p>{FILLER}</p><p>{CLAIM}</p><p>{FILLER}</p></body></html>"
OTHER_PAGE = f"<html><body><p>{FILLER}</p><p>Nothing here about money at all.</p></body></html>"

# Flags for runs against the local fixture server: reach 127.0.0.1, no
# politeness delay, no retries, no Wayback lookups.
LOCAL = ["--allow-private-addresses", "--per-host-delay", "0", "--retries", "0", "--no-wayback"]


def _write(tmp_path, claims, name="claims.json"):
    path = tmp_path / name
    path.write_text(json.dumps(claims), encoding="utf-8")
    return str(path)


def _run_json(capsys, argv):
    code = vc.main(argv + ["--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


# ── Input validation: exit 2, every problem listed, nothing fetched ─────────

@pytest.mark.parametrize("raw, expected", [
    ("{not json", "invalid JSON at line 1"),
    ('{"claim": "x", "source_url": "https://a.test"}', "top level must be a JSON array"),
    ("[1]", "[0]: expected an object, got a JSON number"),
    ('[{"source_url": "https://a.test"}]', "[0]: missing required field 'claim'"),
    ('[{"claim": "x"}]', "[0]: missing required field 'source_url'"),
    ('[{"claim": 5, "source_url": "https://a.test"}]', "'claim' must be a string, got a JSON number"),
    ('[{"claim": "   ", "source_url": "https://a.test"}]', "'claim' is empty"),
    ('[{"id": true, "claim": "x", "source_url": "https://a.test"}]', "'id' must be a string or integer"),
    ('[{"id": [1], "claim": "x", "source_url": "https://a.test"}]', "'id' must be a string or integer"),
])
def test_invalid_input_exits_2_with_a_clear_message(tmp_path, capsys, raw, expected):
    path = tmp_path / "claims.json"
    path.write_text(raw, encoding="utf-8")
    assert vc.main([str(path)]) == vc.EXIT_USAGE
    err = capsys.readouterr().err
    assert "invalid input" in err
    assert expected in err


def test_every_problem_is_reported_in_one_pass(tmp_path, capsys):
    path = _write(tmp_path, [{"claim": "x"}, {"source_url": "https://a.test"}, "nope"])
    assert vc.main([path]) == vc.EXIT_USAGE
    err = capsys.readouterr().err
    assert "[0]: missing required field 'source_url'" in err
    assert "[1]: missing required field 'claim'" in err
    assert "[2]: expected an object, got a JSON string" in err


def test_missing_file_exits_2(tmp_path, capsys):
    assert vc.main([str(tmp_path / "nope.json")]) == vc.EXIT_USAGE
    assert "file not found" in capsys.readouterr().err


def test_non_utf8_input_exits_2(tmp_path, capsys):
    path = tmp_path / "claims.json"
    path.write_bytes(b'[{"claim": "caf\xe9", "source_url": "https://a.test"}]')
    assert vc.main([str(path)]) == vc.EXIT_USAGE
    assert "not valid UTF-8" in capsys.readouterr().err


def test_invalid_input_fetches_nothing(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    path = _write(tmp_path, [{"claim": CLAIM, "source_url": server.url("/")}, {"claim": 1, "source_url": "x"}])
    assert vc.main([path] + LOCAL) == vc.EXIT_USAGE
    assert server.total_hits == 0


def test_stdin_input(server, capsys, monkeypatch):
    server.add("/", body=PAGE)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{"claim": CLAIM, "source_url": server.url("/")}])))
    code, report = _run_json(capsys, ["-"] + LOCAL)
    assert code == vc.EXIT_OK
    assert report["results"][0]["tier"] == "verified"


def test_offline_requires_cache():
    with pytest.raises(SystemExit) as exc:
        vc.main(["claims.json", "--offline"])
    assert exc.value.code == vc.EXIT_USAGE


@pytest.mark.parametrize("flag, value", [
    ("--timeout", "0"), ("--retries", "9"), ("--concurrency", "0"), ("--per-host-delay", "-1"),
    ("--max-bytes", "5"), ("--max-redirects", "99"),
])
def test_out_of_range_flags_exit_2(flag, value):
    with pytest.raises(SystemExit) as exc:
        vc.main(["claims.json", flag, value])
    assert exc.value.code == vc.EXIT_USAGE


# ── Exit codes ──────────────────────────────────────────────────────────────

def test_exit_0_when_everything_is_verified(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    path = _write(tmp_path, [{"id": "a", "claim": CLAIM, "source_url": server.url("/")}])
    code, report = _run_json(capsys, [path] + LOCAL)
    assert code == vc.EXIT_OK
    assert report["summary"]["exit_code"] == 0
    assert report["summary"]["blocking"] is None


def test_exit_1_when_a_claim_is_not_on_the_page(tmp_path, server, capsys):
    server.add("/", body=OTHER_PAGE)
    path = _write(tmp_path, [{"claim": CLAIM, "source_url": server.url("/")}])
    code, report = _run_json(capsys, [path] + LOCAL)
    assert code == vc.EXIT_BLOCKING
    assert report["results"][0]["tier"] == "unsupported"
    assert report["summary"]["blocking"] == "disqualifying"


def test_strict_makes_unchecked_claims_blocking(tmp_path, server, capsys):
    server.add("/doc.pdf", body=b"%PDF-1.7\n" + b"\x00binary" * 100, headers={"Content-Type": "application/pdf"})
    path = _write(tmp_path, [{"claim": CLAIM, "source_url": server.url("/doc.pdf")}])
    code, report = _run_json(capsys, [path] + LOCAL)
    assert code == vc.EXIT_OK
    assert report["results"][0]["tier"] == "unverified"
    assert report["results"][0]["blocking"] is False

    code, report = _run_json(capsys, [path, "--strict"] + LOCAL)
    assert code == vc.EXIT_BLOCKING
    assert report["summary"]["blocking"] == "unchecked"
    assert report["results"][0]["blocking"] is True


def test_exit_4_on_an_internal_error_and_the_run_continues(tmp_path, server, capsys, monkeypatch):
    server.add("/", body=PAGE)
    real = vc.check_source

    def flaky(url, claim, fetcher):
        if "boom" in claim:
            raise RuntimeError("simulated bug")
        return real(url, claim, fetcher)

    monkeypatch.setattr(vc, "check_source", flaky)
    path = _write(tmp_path, [
        {"claim": "boom goes the checker", "source_url": server.url("/")},
        {"claim": CLAIM, "source_url": server.url("/")},
    ])
    code, report = _run_json(capsys, [path] + LOCAL)
    assert code == vc.EXIT_INTERNAL_ERROR
    assert report["summary"]["internal_errors"] == 1
    assert report["results"][0]["tier"] == "unverified"
    assert "simulated bug" in report["results"][0]["note"]
    assert report["results"][1]["tier"] == "verified"


def test_blocking_wins_over_internal_error(tmp_path, server, capsys, monkeypatch):
    server.add("/", body=OTHER_PAGE)
    real = vc.check_source
    monkeypatch.setattr(vc, "check_source", lambda u, c, f: (_ for _ in ()).throw(RuntimeError("x"))
                        if c == "boom" else real(u, c, f))
    path = _write(tmp_path, [{"claim": "boom", "source_url": server.url("/")},
                             {"claim": CLAIM, "source_url": server.url("/")}])
    code, _ = _run_json(capsys, [path] + LOCAL)
    assert code == vc.EXIT_BLOCKING


def test_unwritable_annotate_out_is_reported(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    blocker = tmp_path / "file"
    blocker.write_text("")
    path = _write(tmp_path, [{"claim": CLAIM, "source_url": server.url("/")}])
    code = vc.main([path, "--annotate-out", str(blocker / "out.json")] + LOCAL)
    captured = capsys.readouterr()
    assert code == vc.EXIT_INTERNAL_ERROR
    assert "could not write" in captured.err
    assert "Annotated JSON written" not in captured.out


def test_annotate_out_adds_tier_fields(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    path = _write(tmp_path, [{"id": 7, "claim": CLAIM, "source_url": server.url("/"), "extra": "kept"}])
    out = tmp_path / "nested" / "out.json"
    assert vc.main([path, "--annotate-out", str(out)] + LOCAL) == vc.EXIT_OK
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written[0]["verification_tier"] == "verified"
    assert written[0]["extra"] == "kept"
    assert set(written[0]) >= {"verification_note", "verified_at"}


# ── --json schema ──────────────────────────────────────────────────────────

def test_json_report_schema_is_stable(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    path = _write(tmp_path, [{"id": "x1", "claim": CLAIM, "source_url": server.url("/")},
                             {"claim": "y", "source_url": "ftp://example.com/y"}])
    code, report = _run_json(capsys, [path] + LOCAL)
    assert set(report) == {"schema_version", "cited_version", "checked_at", "options", "summary", "results"}
    assert report["schema_version"] == 1
    assert report["cited_version"] == vc.VERSION
    assert report["checked_at"].endswith("Z")
    assert report["options"] == {"strict": False, "offline": False, "wayback": False}
    summary = report["summary"]
    assert set(summary) == {"total", "counts", "blocking", "offline_misses", "internal_errors", "exit_code"}
    assert list(summary["counts"]) == ["unsupported", "broken", "low_match", "unverified", "snippet_only", "verified"]
    assert summary["total"] == 2 and summary["exit_code"] == code == 1
    first, second = report["results"]
    assert set(first) == {"index", "id", "claim", "source_url", "tier", "label", "blocking", "note", "quoted",
                          "topical", "checked_against", "http_status", "fetched_url", "snapshot_url"}
    assert first["id"] == "x1" and first["checked_against"] == "live" and first["http_status"] == 200
    assert second["id"] == "#1" and second["tier"] == "broken" and second["http_status"] is None


def test_table_output_is_the_default(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    path = _write(tmp_path, [{"id": "a", "claim": CLAIM, "source_url": server.url("/")}])
    vc.main([path] + LOCAL)
    out = capsys.readouterr().out
    assert out.startswith("ID")
    assert "1 claims checked" in out


# ── Cache and offline mode ─────────────────────────────────────────────────

def test_cache_then_offline_reproduces_the_run_without_network(tmp_path, server, capsys):
    server.add("/a", body=PAGE)
    server.add("/b", body=OTHER_PAGE)
    server.add("/gone", 404, body="missing")
    cache = tmp_path / "cache"
    path = _write(tmp_path, [{"claim": CLAIM, "source_url": server.url("/a")},
                             {"claim": CLAIM, "source_url": server.url("/b")},
                             {"claim": CLAIM, "source_url": server.url("/gone")}])
    code1, online = _run_json(capsys, [path, "--cache", str(cache)] + LOCAL)
    hits = server.total_hits
    assert hits == 3
    server.stop()  # nothing to talk to any more

    code2, offline = _run_json(capsys, [path, "--cache", str(cache), "--offline"] + LOCAL)
    assert code2 == code1 == vc.EXIT_BLOCKING
    assert [r["tier"] for r in offline["results"]] == [r["tier"] for r in online["results"]] \
        == ["verified", "unsupported", "broken"]
    assert offline["options"]["offline"] is True
    assert offline["summary"]["offline_misses"] == 0


def test_offline_cache_miss_exits_3(tmp_path, capsys):
    path = _write(tmp_path, [{"claim": CLAIM, "source_url": "https://example.com/never-cached"}])
    code, report = _run_json(capsys, [path, "--cache", str(tmp_path / "empty"), "--offline"])
    assert code == vc.EXIT_OFFLINE_MISS
    assert report["results"][0]["tier"] == "unverified"
    assert report["summary"]["offline_misses"] == 1


def test_transient_failures_are_not_cached(tmp_path, server):
    server.add("/flaky", 503, body="busy")
    fetcher = vc.Fetcher(policy=FetchPolicy(allow_private=True), retries=0, per_host_delay=0,
                         cache_dir=tmp_path / "c")
    assert fetcher.page(server.url("/flaky")).status == 503
    assert list(tmp_path.glob("c/*.json")) == []


def test_corrupt_cache_file_is_ignored(tmp_path, server):
    server.add("/", body=PAGE)
    cache = tmp_path / "c"
    first = vc.Fetcher(policy=FetchPolicy(allow_private=True), retries=0, per_host_delay=0, cache_dir=cache)
    first.page(server.url("/"))
    (entry,) = cache.glob("*.json")
    entry.write_text("{corrupt", encoding="utf-8")
    second = vc.Fetcher(policy=FetchPolicy(allow_private=True), retries=0, per_host_delay=0, cache_dir=cache)
    assert second.page(server.url("/")).status == 200
    assert server.hits["/"] == 2


# ── Retries, backoff, Retry-After ──────────────────────────────────────────

def _flaky_route(server, path, failures, status=503, headers=None):
    state = {"n": 0}

    def body(handler):
        state["n"] += 1
        if state["n"] <= failures:
            handler.send_response(status)
            for key, value in (headers or {}).items():
                handler.send_header(key, value)
            handler.send_header("Content-Length", "4")
            handler.end_headers()
            handler.wfile.write(b"busy")
            return
        data = PAGE.encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    server.add(path, body=body)
    return state


def _fetcher(sleeps, **kw):
    kw.setdefault("retries", 2)
    return vc.Fetcher(policy=kw.pop("policy", FetchPolicy(allow_private=True)), per_host_delay=0,
                      sleep=sleeps.append, **kw)


def test_5xx_is_retried_with_exponential_backoff(server):
    state = _flaky_route(server, "/f", failures=2)
    sleeps: list[float] = []
    result = _fetcher(sleeps, backoff=1.0).page(server.url("/f"))
    assert result.status == 200
    assert state["n"] == 3
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 1.25 and 2.0 <= sleeps[1] <= 2.25


def test_retries_give_up_after_the_limit(server):
    state = _flaky_route(server, "/f", failures=10)
    sleeps: list[float] = []
    result = _fetcher(sleeps, retries=1).page(server.url("/f"))
    assert result.status == 503
    assert state["n"] == 2


def test_retry_after_is_honored(server):
    _flaky_route(server, "/r", failures=1, status=429, headers={"Retry-After": "3"})
    sleeps: list[float] = []
    assert _fetcher(sleeps).page(server.url("/r")).status == 200
    assert sleeps == [3.0]


def test_long_retry_after_is_not_waited_for(server):
    state = _flaky_route(server, "/r", failures=1, status=503, headers={"Retry-After": "3600"})
    sleeps: list[float] = []
    assert _fetcher(sleeps).page(server.url("/r")).status == 503
    assert sleeps == [] and state["n"] == 1


def test_404_is_not_retried(server):
    server.add("/nope", 404, body="no")
    sleeps: list[float] = []
    assert _fetcher(sleeps).page(server.url("/nope")).status == 404
    assert sleeps == [] and server.hits["/nope"] == 1


def test_bot_walled_429_is_not_retried(server):
    state = _flaky_route(server, "/post", failures=5, status=429)
    sleeps: list[float] = []
    policy = FetchPolicy(allow_hosts=frozenset({"x.com"}))
    with fake_dns({"x.com": "127.0.0.1"}):
        result = _fetcher(sleeps, policy=policy).page(server.url("/post", host="x.com"))
    assert result.status == 429
    assert state["n"] == 1 and sleeps == []


def test_blocked_url_is_not_retried():
    sleeps: list[float] = []
    result = vc.Fetcher(per_host_delay=0, sleep=sleeps.append).page("http://127.0.0.1:9/")
    assert result.blocked and sleeps == []


# ── Politeness and de-duplication ──────────────────────────────────────────

def test_host_throttle_spaces_requests_to_the_same_host():
    now = [100.0]
    sleeps: list[float] = []
    throttle = vc.HostThrottle(1.5, clock=lambda: now[0], sleep=sleeps.append)
    throttle.wait("a.test")
    throttle.wait("b.test")
    throttle.wait("a.test")
    throttle.wait("a.test")
    assert sleeps == [1.5, 3.0]


def test_host_throttle_off_when_delay_is_zero():
    sleeps: list[float] = []
    throttle = vc.HostThrottle(0, sleep=sleeps.append)
    for _ in range(3):
        throttle.wait("a.test")
    assert sleeps == []


def test_same_url_is_fetched_once_per_run(tmp_path, server, capsys):
    server.add("/", body=PAGE)
    claims = [{"claim": CLAIM, "source_url": server.url("/")} for _ in range(8)]
    path = _write(tmp_path, claims)
    code, report = _run_json(capsys, [path, "--concurrency", "8"] + LOCAL)
    assert code == vc.EXIT_OK
    assert server.hits["/"] == 1
    assert [r["index"] for r in report["results"]] == list(range(8))


# ── Decoding and content types ─────────────────────────────────────────────

def test_cp1252_page_without_charset_is_decoded(server):
    body = ("<p>" + "Café crème – naïve résumé " * 20 + "</p>").encode("cp1252")
    server.add("/w", body=body, headers={"Content-Type": "text/html"})
    status, text = vc.fetch_text(server.url("/w"), 3, FetchPolicy(allow_private=True))
    assert status == 200
    assert "Café crème – naïve" in text


def test_meta_charset_is_used_when_the_header_has_none(server):
    body = ('<meta charset="iso-8859-7"><p>' + "Αθήνα " * 60 + "</p>").encode("iso-8859-7")
    server.add("/g", body=body, headers={"Content-Type": "text/html"})
    _, text = vc.fetch_text(server.url("/g"), 3, FetchPolicy(allow_private=True))
    assert "Αθήνα" in text


@pytest.mark.parametrize("body, header, expected", [
    (b"\xef\xbb\xbfhello", None, "hello"),
    ("héllo".encode("utf-16"), None, "héllo"),
    ("héllo".encode("latin-1"), "latin-1", "héllo"),
    (b"hello", "rot13", "hello"),          # a non-text codec name is ignored
    (b"hello", "no-such-codec", "hello"),
])
def test_decode_body(body, header, expected):
    assert decode_body(body, header) == expected


def test_pdf_and_binary_are_not_parsed(server):
    server.add("/p", body=b"%PDF-1.4 whatever", headers={"Content-Type": "application/octet-stream"})
    server.add("/z", body=b"PK\x03\x04\x00\x00", headers={"Content-Type": "application/zip"})
    fetcher = vc.Fetcher(policy=FetchPolicy(allow_private=True), retries=0, per_host_delay=0)
    pdf, zipped = fetcher.page(server.url("/p")), fetcher.page(server.url("/z"))
    assert (pdf.kind, pdf.text) == ("pdf", "")
    assert (zipped.kind, zipped.text) == ("binary", "")
    verdict = vc.check_source(server.url("/z"), CLAIM, vc.Fetcher(policy=FetchPolicy(allow_private=True),
                                                                    retries=0, per_host_delay=0,
                                                                    use_wayback=False))
    assert verdict.tier == "unverified"
    assert "application/zip" in verdict.note


def test_plain_text_pages_are_checked(server):
    server.add("/t", body=f"{FILLER}\n{CLAIM}\n", headers={"Content-Type": "text/plain; charset=utf-8"})
    verdict = vc.check_source(server.url("/t"), CLAIM, vc.Fetcher(policy=FetchPolicy(allow_private=True),
                                                                    retries=0, per_host_delay=0))
    assert verdict.tier == "verified"
