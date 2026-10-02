"""The README, SECURITY.md and SKILL.md restate facts the code owns: tiers
and what blocks, exit codes, the JSON report shape, flag defaults and the
blocked address ranges. These tests fail when the two drift apart."""

from __future__ import annotations

import ipaddress
import json
import re

import safe_fetch
import verify_claims as vc
from conftest import FIXTURES
from tiering_core import TIER_LABELS, TIERS, blocking_reason, is_blocking

ROOT = FIXTURES.parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
SECURITY = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
SKILL = (ROOT / "skills" / "cited" / "SKILL.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^#+ {re.escape(heading)}\n(.*?)(?=^#+ |\Z)", text, re.S | re.M)
    assert match, f"no '{heading}' section"
    return match.group(1)


def _table(text: str, heading: str) -> list[dict[str, str]]:
    lines = [line for line in _section(text, heading).splitlines() if line.startswith("|")]
    header, _, *rows = ([cell.strip() for cell in line.strip("|").split("|")] for line in lines)
    return [dict(zip(header, row, strict=True)) for row in rows]


def _code(cell: str) -> str:
    return cell.strip().strip("`")


def test_readme_tier_table_matches_the_code():
    rows = {_code(row["Tier"]): row for row in _table(README, "Tiers")}
    assert set(rows) == set(TIERS)
    for tier, row in rows.items():
        assert row["Label"] == TIER_LABELS[tier], tier
        assert row["Blocks by default"] == ("yes" if is_blocking(tier) else "no"), tier
        assert row["Blocks with `--strict`"] == ("yes" if is_blocking(tier, strict=True) else "no"), tier


def test_readme_exit_codes_match_the_code():
    rows = {int(row["Code"]): row["Meaning"].replace("`", "") for row in _table(README, "Exit codes")}
    assert rows == vc.EXIT_CODES


def test_help_lists_every_exit_code():
    text = " ".join(vc.build_parser().format_help().split())
    for code, meaning in vc.EXIT_CODES.items():
        assert f"{code} {meaning}" in text


def test_readme_options_table_matches_the_parser():
    documented: dict[str, str] = {}
    for row in _table(README, "Options"):
        for flag in re.findall(r"--[a-z-]+", row["Flag"]):
            documented[flag] = row["Default"]
    parser_flags = {a.option_strings[-1]: a for a in vc.build_parser()._actions
                    if a.option_strings and a.dest not in ("help", "version")}
    assert set(documented) == set(parser_flags)
    for flag, action in parser_flags.items():
        cell = documented[flag]
        if action.const is True or action.default in (None, []):
            assert cell in ("", "off"), flag
        else:
            assert float(cell) == action.default, flag


def _readme_report_example() -> dict:
    block = re.search(r"```json\n(.*?)```", _section(README, "JSON report"), re.S)
    return json.loads(block.group(1))


def test_readme_json_example_has_the_real_report_shape(tmp_path, server, capsys):
    server.add("/", body="<p>" + "Example Corporation reported revenue of 12 million dollars. " * 10 + "</p>")
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([{"id": "a", "claim": "Example Corporation reported revenue of 12 million dollars.",
                                   "source_url": server.url("/")}]), encoding="utf-8")
    vc.main([str(claims), "--json", "--allow-private-addresses", "--per-host-delay", "0", "--retries", "0"])
    report = json.loads(capsys.readouterr().out)
    example = _readme_report_example()

    assert set(example) == set(report)
    assert example["schema_version"] == report["schema_version"] == vc.SCHEMA_VERSION
    assert set(example["options"]) == set(report["options"])
    assert set(example["summary"]) == set(report["summary"])
    assert list(example["summary"]["counts"]) == list(report["summary"]["counts"]) == list(TIERS)
    assert set(example["results"][0]) == set(report["results"][0])


def test_readme_names_every_blocking_reason():
    section = _section(README, "JSON report")
    reasons = {blocking_reason({tier: 1}, strict=True) for tier in TIERS} - {None}
    for reason in reasons:
        assert f'`"{reason}"`' in section, reason


def test_security_lists_exactly_the_blocked_networks():
    row = next(line for line in SECURITY.splitlines() if line.startswith("| Addresses |"))
    v4_text, v6_text = row.split("Blocked IPv4:", 1)[1].split("Blocked IPv6:", 1)
    v4 = {ipaddress.ip_network(n) for n in re.findall(r"`([0-9.]+/\d+)`", v4_text)}
    v6 = {ipaddress.ip_network(n) for n in re.findall(r"`([0-9a-f:]+/\d+)`", v6_text)}
    assert v4 == set(safe_fetch._BLOCKED_V4)
    assert v6 == set(safe_fetch._BLOCKED_V6)


def test_skill_covers_every_tier():
    for tier in TIERS:
        assert f"`{tier}`" in SKILL, tier
