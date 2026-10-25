# Run log

Real documents driven over HTTP against the local server (`python3 hosted/webapp.py`, local storage, test access codes). No secrets here.

## Run 1, 2026-10-09, before any fixes (c082b04)

| Input | Mode | HTTP | Time | Result |
|---|---|---|---|---|
| instinct-muse-mechanisms-2026-10-06.md (19.8 KB, 21 distinct http(s) URLs) | Free link check | 200 | 0.02 s | "0 link(s) checked", "No http(s) links were found" |
| five-bets-distribution-productization-2026-10-06.md (6 KB, 0 http(s) URLs, 8 scheme-less `(domain/path)` refs) | Certificate | 400 | 0.01 s | "No checkable claims were found" |

What broke: the extractor only understands `[text](url)` links. Real research notes cite in three other ways, and none were seen:

- a table row whose last cell is a bare URL: `| claim | [V] | https://instinct.com |`
- a bare URL or autolink after the claim, often in brackets: `... [S, single source: https://stacktr.ee/...]`
- a scheme-less reference in parentheses: `(help.luma.com/p/luma-api)`

Across the 12 research notes in the same folder, 11 cite only with bare URLs; one uses Markdown links. So on the owner's own documents, both modes did nothing.

Fixed in e92fdee: the extractor now reads bare URLs (table rows, bracketed tags such as `[S: url]`, autolinks) and scheme-less `(domain/path)` references, which get `https://` and are labelled as inferred. A `## Sources` list stays bibliography and is not scored.

## Run 2, same day, after the extractor fix, still synchronous certificates

| Input | Mode | HTTP | Time | Result |
|---|---|---|---|---|
| instinct-muse | Free | 200 | 2.1 s | 20 links, all OK |
| five-bets | Certificate | 303 | 1.2 s | 8 claims |
| signal-scout-distribution (57 URLs) | Certificate | 303 | 6.8 s | 43 claims, 16 links not checkable |

Fast because these hosts answer quickly; nothing yet stopped a slow draft from running past 60 s in one request.

## Run 3, final code (2e96e22), certificates as polled jobs

Local server, local storage, Python 3.12, home connection. Times are curl `time_total`.

| Input | Mode | Requests | Longest request | Wall time | Result |
|---|---|---|---|---|---|
| instinct-muse (21 URLs) | Free | 1 POST, 200 | 1.3 s | 1.3 s | All 20 distinct sources load |
| five-bets (8 scheme-less refs) | Free | 1 POST, 200 | 1.9 s | 1.9 s | 5 OK, 3 redirected |
| simonwillison.net "Things we learned about LLMs in 2024" (web page URL) | Free | 1 POST, 200 | 5.9 s | 5.9 s | 25 checked (23 OK, 2 redirected), 212 more not checked (free cap) |
| five-bets | Certificate | POST 200, 1 GET | 1.0 s | 1.2 s | 8 claims: 5 not on page, 2 needs review, 1 unverified (PDF) |
| instinct-muse | Certificate | POST 200, 1 GET | 4.2 s | 4.5 s | 27 claims: 2 verified, 15 needs review, 10 not on page; 19 not checkable (the Sources list) |
| signal-scout + scouted-viability + shelfie-complaint-mining, concatenated (128 distinct URLs) | Certificate | POST 200, 4 GETs | 21.1 s | 60.3 s | 113 claims: 1 verified, 25 needs review, 64 not on page, 15 broken, 8 unverified; 33 not checkable |

No 5xx in the server log. Earlier in the same session, on the job-model code before the design commit (84a23ad), also over HTTP: wrong access code 402; delete with a wrong key 403, confirm page 200, delete 200, then the share link 410; the eleventh `POST /check` within ten minutes from one address 429. `recheck.py --id` on the stored instinct-muse run: 27 claims re-checked in 3 s, no verdict changed, exit 0.

What the real documents showed beyond the extractor gap:

- Research notes paraphrase. Most claims came back "Not on page" or "Needs review", not because the sources are fabricated but because a summary row ("Funding: Series C $1B at $10B led by Sequoia...") is not worded like its source. That is what cited is designed to flag, but a buyer should expect a red certificate on notes, and a green one only on prose that quotes its sources.
- A table row with two URLs checks the whole row against each URL, so each half fails on the other half's specifics. Splitting a row's claim across its sources is not built.
- A few sources are PDFs (cited reads HTML and text only) and come back Unverified.

## Run 4: claims scoped to the clause nearest their citation

The biggest source of false reds in run 3 was compound text checked whole against each of its sources. Example from line 24 of the three-note draft: "Directories: OpenAI bans ...; Muse bans ...; Anthropic's directory policy 1.C requires ..." was checked in full against the OpenAI, Muse and Anthropic pages, and all three came back "Not on page" because each page lacked the other two companies' specifics.

The extractor now cuts bare-URL text into clauses at `;`, sentence ends and table cells. Each clause goes to its nearest citation: the one that follows it, or the one before it for text after a clause's last citation. A segment under four words borrows the next stretch, then earlier clauses, without crossing another citation. A lone citation with no split still gets its whole sentence. A list of URLs with no claim between them ("Example URLs: url (date); url (date)") is now excluded instead of checked. Verdict rules are unchanged. Sentence splitting also now breaks after bold lead-ins ("**...report.** Lemon Squeezy ...").

Measured with the CLI on the same draft (signal-scout + scouted-viability + shelfie-complaint-mining, concatenated), both runs offline from one page cache, so every source returned identical bytes and only the extractor differs:

| | Claims | Verified | Needs review | Not on page | Broken | Unverified | Not checkable |
|---|---|---|---|---|---|---|---|
| Before | 113 | 1 | 25 | 62 | 16 | 9 | 33 |
| After | 104 | 1 | 56 | 36 | 9 | 2 | 42 |

Where the difference comes from:

- 88 claim/source pairs exist in both runs. 51 have a new, narrower claim: 23 moved from Not on page to Needs review, 3 went the other way (for example "Vendor pays $12k to $24k/yr or $6k + 10% ACV" now replaces the label "Proven (VC perks portals)" and its numbers are not on the page), and 25 kept their tier. The 37 with unchanged claim text kept their verdicts exactly.
- 25 pairs left: 24 are "Example URLs:" lists of dated links (8 broken, 7 unverified, 9 not on page before), now excluded as having no claim, and 1 is a two-source price cell too short to check alone.
- 16 pairs are new: rows whose earlier claim was the row label and is now the URL's own cell (11 needs review, 4 not on page, 1 broken).

Verified did not move. These notes paraphrase their sources, so the best a scoped claim gets is usually Needs review, a person should read it, rather than Not on page, the fabrication signal. That is the change the scoping was meant to make: fewer false fabrication flags, with no new passes.
