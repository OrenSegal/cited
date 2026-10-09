"""The 30-day re-check: re-runs a stored certificate run and reports exactly
the verdicts that changed. All traffic goes to the local fixture server."""

from __future__ import annotations

import json

from test_hosted import LOCAL, OTHER, PAGE, _app, _cert_draft, _finish, _start

import recheck
import storage as st
from fetcher import Fetcher


def _stored_run(tmp_path, server):
    app = _app(tmp_path, LOCAL)
    cert_id, _ = _start(app, _cert_draft(server))
    _finish(app, cert_id)
    return cert_id, json.loads((tmp_path / "data" / "certs" / f"{cert_id}.json").read_text())


def test_unchanged_pages_report_no_change(tmp_path, server):
    _, run = _stored_run(tmp_path, server)
    report = recheck.recheck(run, Fetcher(policy=LOCAL, timeout=5, retries=0, per_host_delay=0, use_wayback=False))
    assert report["changed"] == []
    assert report["counts"] == run["counts"]


def test_a_page_that_drifted_is_reported(tmp_path, server):
    _, run = _stored_run(tmp_path, server)
    server.add("/report", body=OTHER)  # the claim's page no longer says it
    server.add("/gone", body=PAGE)     # the dead source came back
    report = recheck.recheck(run, Fetcher(policy=LOCAL, timeout=5, retries=0, per_host_delay=0, use_wayback=False))
    moves = sorted((c["was"], c["now"]) for c in report["changed"])
    assert ("verified", "unsupported") in moves
    assert any(was == "broken" for was, _ in moves)
    assert report["previous_checked_at"] == run["checked_at"]


def test_cli_reads_a_file_and_exits_by_result(tmp_path, server, capsys):
    _, run = _stored_run(tmp_path, server)
    path = tmp_path / "run.json"
    path.write_text(json.dumps(run))
    # The CLI uses the public policy, so the fixture sources (127.0.0.1) are
    # refused: every non-broken verdict becomes Broken, and nothing is fetched.
    hits = server.total_hits
    assert recheck.main([str(path), "--no-wayback", "--timeout", "2"]) == recheck.EXIT_CHANGED
    assert server.total_hits == hits
    out = capsys.readouterr().out
    assert "Verified -> Broken source" in out

    assert recheck.main([str(tmp_path / "missing.json")]) == recheck.EXIT_USAGE


def test_cli_loads_by_id_from_storage_and_saves(tmp_path, server, monkeypatch, capsys):
    cert_id, _ = _stored_run(tmp_path, server)
    monkeypatch.setenv("CITED_DATA_DIR", str(tmp_path / "data"))
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET", "VERCEL"):
        monkeypatch.delenv(name, raising=False)
    code = recheck.main(["--id", cert_id, "--save", "--json", "--no-wayback", "--timeout", "2"])
    assert code == recheck.EXIT_CHANGED
    saved = json.loads(st.LocalStorage(tmp_path / "data").get(f"certs/{cert_id}.recheck.json"))
    assert saved["changed"]
    assert json.loads(capsys.readouterr().out)["changed"] == saved["changed"]
    assert recheck.main(["--id", "nonexistent-id-123"]) == recheck.EXIT_USAGE
