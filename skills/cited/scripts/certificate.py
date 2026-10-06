"""Render a run as a standalone HTML certificate. Pure: no I/O.

Ported from receipts (Oren Segal, MIT). The page says: every sourced claim in
this document was re-fetched from the URL it cites at this time, and here is
what each one came back as. The disclaimer sits at the top, not in a footer:
cited proves containment, not truth, and a certificate that lets a reader
believe otherwise is worse, not better.

Every value that came from the input or from a fetched page is escaped.
"""

from __future__ import annotations

import html
from collections import Counter
from typing import Any

from tiering_core import (
    TIER_BROKEN,
    TIER_LABELS,
    TIER_LOW_MATCH,
    TIER_SNIPPET_ONLY,
    TIER_UNSUPPORTED,
    TIER_UNVERIFIED,
    TIER_VERIFIED,
    TIERS,
    blocking_reason,
)

TIER_MEANING = {
    TIER_VERIFIED: "The claim's wording was found on the page it cites.",
    TIER_LOW_MATCH: "The page covers this subject, but not in these words. A person should read it.",
    TIER_SNIPPET_ONLY: "The source blocks automated readers. Not a mark against the claim.",
    TIER_UNVERIFIED: "The page returned no readable text to check against.",
    TIER_UNSUPPORTED: "The source was read and the claim is not in it. This is the fabrication signal.",
    TIER_BROKEN: "The cited page could not be reached, live or archived.",
}

CSS = """
:root{--bg:#fbfaf8;--fg:#16150f;--mut:#6a6558;--line:#e2ded4;--card:#fff;
--ok:#1a7f4b;--warn:#8a6100;--bad:#a32020;--info:#4a4a8a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:940px;margin:0 auto;padding:40px 20px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--mut);font-size:14px;margin:0 0 28px}
.verdict{display:flex;flex-wrap:wrap;gap:14px;align-items:center;background:var(--card);
border:1px solid var(--line);border-left:5px solid var(--vc,var(--ok));border-radius:8px;padding:18px 20px;margin-bottom:8px}
.verdict b{font-size:19px}
.verdict .why{flex:1 1 320px}
.scoreboard{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 8px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 13px;font-size:13px}
.chip b{font-variant-numeric:tabular-nums}
.note{background:#fff8e8;border:1px solid #eddfb8;border-radius:8px;padding:14px 16px;
font-size:13.5px;color:#5a4a1a;margin:22px 0}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:34px 0 10px;font-weight:600}
.claim{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:9px}
.claim .top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.tier{font-size:11.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
padding:2px 8px;border-radius:4px;background:#eee;color:#333;white-space:nowrap}
.t-verified{background:#e4f4ea;color:var(--ok)}
.t-low_match{background:#fdf1dc;color:var(--warn)}
.t-snippet_only{background:#ecebf6;color:var(--info)}
.t-unverified{background:#eeece7;color:var(--mut)}
.t-unsupported,.t-broken{background:#fbe6e6;color:var(--bad)}
.claim q{display:block;margin:2px 0 8px;font-size:14.5px}
.meta{font-size:12.5px;color:var(--mut);display:flex;gap:14px;flex-wrap:wrap;align-items:center;
font-variant-numeric:tabular-nums}
.meta a{color:var(--info);text-decoration:none;word-break:break-all}
.meta a:hover{text-decoration:underline}
.why{font-size:12.5px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
.scroll{overflow-x:auto}
td,th{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:#f0eee8;padding:1px 5px;border-radius:3px}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);font-size:12.5px;color:var(--mut)}
@media print{body{background:#fff}.claim,.verdict{break-inside:avoid}}
"""


def _esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def _verdict(counts: Counter, strict: bool) -> tuple[str, str, str]:
    reason = blocking_reason(counts, strict=strict)
    bad = counts[TIER_UNSUPPORTED] + counts[TIER_BROKEN]
    if reason == "disqualifying":
        return ("#a32020", f"{bad} claim(s) did not check out",
                "At least one claim is not on the page it cites, or its source is unreachable. "
                "Fix these before this goes out.")
    if reason == "needs_review":
        return ("#8a6100", f"{counts[TIER_LOW_MATCH]} claim(s) need a person to check them",
                "No fabricated or dead sources, but some claims only share vocabulary with their page. "
                "Read each against its source, or tighten it to what the page says.")
    unchecked = counts[TIER_UNVERIFIED] + counts[TIER_SNIPPET_ONLY]
    if unchecked:
        title = f"{unchecked} claim(s) were never checked" + ("" if reason else ", nothing else blocks")
        return ("#8a6100", title,
                f"{unchecked} source(s) could not be read as text (a PDF, a thin page, or a site that blocks "
                "automated readers). Those claims need a person.")
    return ("#1a7f4b", "Every claim was quoted from its cited source",
            "Each sourced claim was re-fetched and its wording found on the page.")


def render(
    doc_name: str,
    rows: list[dict[str, Any]],
    checked_at: str,
    *,
    excluded: list[dict[str, Any]] | None = None,
    links_total: int | None = None,
    strict: bool = False,
    version: str = "",
) -> str:
    """`rows` are the `results` of the JSON report; `excluded` are links the
    extractor refused to check ({line, link_text, source_url, reason})."""
    excluded = excluded or []
    counts: Counter = Counter(row["tier"] for row in rows)
    colour, headline, why = _verdict(counts, strict)

    chips = "".join(f'<span class="chip">{_esc(TIER_LABELS[t])} <b>{counts[t]}</b></span>'
                    for t in TIERS if counts.get(t))
    if excluded:
        chips += f'<span class="chip">Not checkable <b>{len(excluded)}</b></span>'

    order = {tier: i for i, tier in enumerate(TIERS)}
    cards = []
    for row in sorted(rows, key=lambda r: (order.get(r["tier"], 99), r.get("line") or 0)):
        where = f"line {row['line']} · " if row.get("line") else ""
        repaired = (' · <span class="why">claim recovered from the preceding sentence</span>'
                    if row.get("bucket") == "repaired" else "")
        note = f'<div class="meta">{_esc(row.get("note"))}</div>' if row.get("note") else ""
        cards.append(f"""<div class="claim">
  <div class="top"><span class="tier t-{_esc(row['tier'])}">{_esc(TIER_LABELS.get(row['tier'], row['tier']))}</span>
  <span class="why">{_esc(TIER_MEANING.get(row['tier'], ''))}</span></div>
  <q>{_esc(row['claim'])}</q>
  <div class="meta"><a href="{_esc(row['source_url'])}">{_esc(row['source_url'])}</a></div>
  <div class="meta">{where}quoted {row.get('quoted', 0):.0%} · vocabulary {row.get('topical', 0):.0%}
  · checked against {_esc(row.get('checked_against', 'none'))}{repaired}</div>
  {note}
</div>""")

    ex_rows = "".join(
        f"<tr><td>line {_esc(p.get('line'))}</td><td><code>[{_esc(str(p.get('link_text', ''))[:44])}]</code></td>"
        f"<td>{_esc(p.get('reason'))}</td></tr>" for p in excluded)
    ex_block = f"""<h2>Links deliberately not checked ({len(excluded)})</h2>
<p class="why">A containment check asks whether a claim's words are on the page it cites.
A link with no claim attached (a bare "here", a bibliography entry, a relative path) can never
pass that check. Reporting these as failures would be a broken check, not a finding, so they are
listed rather than scored.</p>
<div class="scroll"><table><tr><th>Where</th><th>Link</th><th>Why it was skipped</th></tr>
{ex_rows}</table></div>""" if excluded else ""

    found = f"{links_total} link(s) found · " if links_total is not None else ""
    mode = " · strict" if strict else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Source check: {_esc(doc_name)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>Source check</h1>
<p class="sub"><code>{_esc(doc_name)}</code> · {found}{len(rows)} claim(s) checked · {_esc(checked_at)}{mode}</p>

<div class="verdict" style="--vc:{colour}">
  <b>{_esc(headline)}</b><span class="why">{_esc(why)}</span>
</div>
<div class="scoreboard">{chips}</div>

<div class="note"><b>What this proves, and what it does not.</b> Every claim below was re-fetched
from the URL it cites and checked for whether the claim's own words, numbers and names appear on that
page. That is <i>containment</i>, not truth. A claim can be quoted perfectly from a source that is
itself wrong, and this check would call it verified. It catches invented sources, misattributed
quotes, dead links and drifted pages, not bad sources honestly cited.</div>

<h2>Claims checked ({len(rows)})</h2>
{''.join(cards) or '<p class="why">No checkable claims were found in this document.</p>'}
{ex_block}
<footer>Generated by cited {_esc(version)}. Pages were as described above when each was fetched,
and may have changed since.</footer>
</div></body></html>
"""
