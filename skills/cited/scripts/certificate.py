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

# Shared with the hosted pages (hosted/webapp.py), so a certificate and the
# form that made it read as one product. Light and dark follow the reader's
# system setting; tier colours keep one meaning in both.
CSS = """
:root{color-scheme:light dark;
--bg:#f7f5f0;--fg:#1b1a16;--mut:#625c4c;--line:#e2ddd0;--card:#fffdf8;--sunk:#efece4;
--bad:#b42318;--warn:#8a5a00;--ok:#17734a;--info:#47469a;--unk:#6b6657;--link:#2f4fa8;--focus:#2f4fa8;
--serif:"Iowan Old Style","Charter","Source Serif Pro","Georgia",serif;
--sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{
--bg:#13120f;--fg:#ebe7dd;--mut:#a39c8b;--line:#2d2b25;--card:#1b1a16;--sunk:#22201b;
--bad:#ff8b7b;--warn:#f0b84a;--ok:#5fcf98;--info:#aeadf2;--unk:#a39c8b;--link:#9db4ff;--focus:#9db4ff}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 var(--sans);font-variant-numeric:tabular-nums}
::selection{background:color-mix(in srgb,var(--link) 28%,transparent)}
a{color:var(--link);text-underline-offset:.18em;text-decoration-thickness:1px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}
code{font:.82em var(--mono);background:var(--sunk);padding:.1em .4em;border-radius:4px;overflow-wrap:anywhere}
.wrap{max-width:860px;margin:0 auto;padding:48px 16px 40px}
h1,h2{font-family:var(--serif);font-weight:600;text-wrap:balance}
h1{font-size:clamp(28px,6vw,40px);line-height:1.12;letter-spacing:-.015em;margin:0 0 12px}
.lede{font-size:17px;max-width:62ch;margin:0 0 14px}
.sub{color:var(--mut);font-size:14px;margin:0 0 28px}
.v-bad{color:var(--bad)}.v-warn{color:var(--warn)}.v-ok{color:var(--ok)}
.tally{display:flex;height:10px;border-radius:5px;overflow:hidden;gap:2px;background:var(--sunk);margin:0 0 12px}
.tally span{display:block;min-width:4px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;list-style:none;padding:0;margin:0 0 32px;font-size:14px}
.legend a{color:var(--fg);text-decoration:none}
.legend a:hover b{text-decoration:underline}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;vertical-align:-1px}
.c-unsupported,.c-broken{--tc:var(--bad)}.c-low_match{--tc:var(--warn)}.c-snippet_only{--tc:var(--info)}
.c-unverified{--tc:var(--unk)}.c-verified{--tc:var(--ok)}.c-excluded{--tc:var(--line)}
.tally .c-unsupported,.legend .c-unsupported i{background:var(--bad)}
.tally .c-broken,.legend .c-broken i{background:color-mix(in srgb,var(--bad) 55%,var(--bg))}
.tally .c-low_match,.legend .c-low_match i{background:var(--warn)}
.tally .c-snippet_only,.legend .c-snippet_only i{background:var(--info)}
.tally .c-unverified,.legend .c-unverified i{background:var(--unk)}
.tally .c-verified,.legend .c-verified i{background:var(--ok)}
.legend .c-excluded i{background:transparent;border:1px solid var(--mut)}
.note{font-size:14px;color:var(--mut);border-top:1px solid var(--line);border-bottom:1px solid var(--line);
padding:14px 0;margin:0 0 8px;max-width:72ch}
.note b{color:var(--fg)}
h2{font-size:22px;margin:44px 0 4px;display:flex;align-items:baseline;gap:10px}
h2 .n{font:600 15px var(--sans);color:var(--tc,var(--mut))}
h2::before{content:"";width:10px;height:10px;border-radius:2px;background:var(--tc,var(--mut));flex:none;align-self:center}
.meaning{color:var(--mut);font-size:14.5px;margin:0 0 14px;max-width:68ch}
.claims{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.claim{padding:16px 0;border-bottom:1px solid var(--line)}
.claim q{display:block;font-size:16px;margin:0 0 6px;max-width:72ch;quotes:none}
.src{font-size:14px;overflow-wrap:anywhere}
.meta{font-size:13px;color:var(--mut);margin-top:4px}
.why{font-size:13px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:14px}
.scroll{overflow-x:auto}
td,th{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:13px}
footer{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);font-size:13px;color:var(--mut)}
@media print{:root{color-scheme:light}body{background:#fff}.claim{break-inside:avoid}}
"""


def _esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def _link(url: Any) -> str:
    """Only http(s) becomes clickable. A `javascript:` or `data:` URL from a
    claims file is shown as text, never as an href someone can click."""
    u = "" if url is None else str(url).strip()
    if u.lower().startswith(("http://", "https://")):
        return f'<a href="{_esc(u)}">{_esc(u)}</a>'
    return f"<code>{_esc(u)}</code>"


def _verdict(counts: Counter, strict: bool) -> tuple[str, str, str]:
    reason = blocking_reason(counts, strict=strict)
    bad = counts[TIER_UNSUPPORTED] + counts[TIER_BROKEN]
    if reason == "disqualifying":
        return ("bad", f"{bad} claim(s) did not check out",
                "At least one claim is not on the page it cites, or its source is unreachable. "
                "Fix these before this goes out.")
    if reason == "needs_review":
        return ("warn", f"{counts[TIER_LOW_MATCH]} claim(s) need a person to check them",
                "No fabricated or dead sources, but some claims only share vocabulary with their page. "
                "Read each against its source, or tighten it to what the page says.")
    unchecked = counts[TIER_UNVERIFIED] + counts[TIER_SNIPPET_ONLY]
    if unchecked:
        title = f"{unchecked} claim(s) were never checked" + ("" if reason else ", nothing else blocks")
        return ("warn", title,
                f"{unchecked} source(s) could not be read as text (a PDF, a thin page, or a site that blocks "
                "automated readers). Those claims need a person.")
    return ("ok", "Every claim was quoted from its cited source",
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
    footer_link: str | None = None,
) -> str:
    """`rows` are the `results` of the JSON report; `excluded` are links the
    extractor refused to check ({line, link_text, source_url, reason}).
    `footer_link`, an http(s) URL, signs the footer "Checked by cited" with
    cited linked to it, for certificates hosted where readers can find it."""
    excluded = excluded or []
    counts: Counter = Counter(row["tier"] for row in rows)
    tone, headline, why = _verdict(counts, strict)

    present = [t for t in TIERS if counts.get(t)]
    tally = "".join(f'<span class="c-{t}" style="flex:{counts[t]}"></span>' for t in present)
    legend = "".join(f'<li class="c-{t}"><a href="#g-{t}"><i></i>{_esc(TIER_LABELS[t])} <b>{counts[t]}</b></a></li>'
                     for t in present)
    if excluded:
        legend += f'<li class="c-excluded"><a href="#g-excluded"><i></i>Not checkable <b>{len(excluded)}</b></a></li>'
    summary = ", ".join(f"{counts[t]} {TIER_LABELS[t].lower()}" for t in present)

    groups = []
    for tier in present:
        items = []
        for row in sorted((r for r in rows if r["tier"] == tier), key=lambda r: r.get("line") or 0):
            where = f"line {row['line']} · " if row.get("line") else ""
            repaired = " · claim recovered from the preceding sentence" if row.get("bucket") == "repaired" else ""
            note = f'<div class="meta">{_esc(row.get("note"))}</div>' if row.get("note") else ""
            items.append(f"""<li class="claim">
  <q>{_esc(row['claim'])}</q>
  <div class="src">{_link(row['source_url'])}</div>
  <div class="meta">{where}quoted {row.get('quoted', 0):.0%} · vocabulary {row.get('topical', 0):.0%}
  · checked against {_esc(row.get('checked_against', 'none'))}{repaired}</div>
  {note}
</li>""")
        groups.append(f"""<section class="c-{_esc(tier)}" id="g-{_esc(tier)}">
<h2>{_esc(TIER_LABELS.get(tier, tier))} <span class="n">{counts[tier]}</span></h2>
<p class="meaning">{_esc(TIER_MEANING.get(tier, ''))}</p>
<ol class="claims">{''.join(items)}</ol>
</section>""")

    ex_rows = "".join(
        f"<tr><td>line {_esc(p.get('line'))}</td><td><code>[{_esc(str(p.get('link_text', ''))[:44])}]</code></td>"
        f"<td>{_esc(p.get('reason'))}</td></tr>" for p in excluded)
    ex_block = f"""<section class="c-excluded" id="g-excluded">
<h2>Links deliberately not checked ({len(excluded)})</h2>
<p class="meaning">A containment check asks whether a claim's words are on the page it cites.
A link with no claim attached (a bare "here", a bibliography entry, a relative path) can never
pass that check. Reporting these as failures would be a broken check, not a finding, so they are
listed rather than scored.</p>
<div class="scroll"><table><tr><th>Where</th><th>Link</th><th>Why it was skipped</th></tr>
{ex_rows}</table></div></section>""" if excluded else ""

    found = f"{links_total} link(s) found · " if links_total is not None else ""
    mode = " · strict" if strict else ""
    byline = "Generated by cited"
    if footer_link and footer_link.lower().startswith(("http://", "https://")):
        byline = f'Checked by <a href="{_esc(footer_link)}">cited</a>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Source check: {_esc(doc_name)}</title>
<style>{CSS}</style></head>
<body><main class="wrap">
<h1 class="v-{tone}">{_esc(headline)}</h1>
<p class="lede">{_esc(why)}</p>
<p class="sub">Source check of <code>{_esc(doc_name)}</code> · {found}{len(rows)} claim(s) checked · {_esc(checked_at)}{mode}</p>
<div class="tally" role="img" aria-label="{_esc(summary or 'no claims')}">{tally}</div>
<ul class="legend">{legend}</ul>

<p class="note"><b>What this proves, and what it does not.</b> Every claim below was re-fetched
from the URL it cites and checked for whether the claim's own words, numbers and names appear on that
page. That is <i>containment</i>, not truth. A claim can be quoted perfectly from a source that is
itself wrong, and this check would call it verified. It catches invented sources, misattributed
quotes, dead links and drifted pages, not bad sources honestly cited.</p>

{''.join(groups) or '<p class="why">No checkable claims were found in this document.</p>'}
{ex_block}
<footer>{byline} {_esc(version)}. Pages were as described above when each was fetched,
and may have changed since.</footer>
</main></body></html>
"""
