# Verify Before Ship

[![CI](https://github.com/OrenSegal/verify-before-ship/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenSegal/verify-before-ship/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-black)](LICENSE)

A Python script (standard library only) plus a Claude Code `SKILL.md` that tells an agent when and how to run it. You give it a list of claims, each with the URL it cites. It fetches each URL and checks whether the claim's words appear on the page. It flags claims whose text, numbers, or names are not on the cited page, so a person can review or drop them before the artifact ships. It does not run on its own and it does not judge whether a claim is true.

Extracted and generalized from [signal-scout](https://github.com/OrenSegal/signal-scout)'s source-verification mechanism (`verify_sources.py`). Same containment-checking engine, decoupled from that project's lead-gen-specific schema so it works on any `(claim, source_url)` pair: leads, research citations, podcast quotes, changelog claims, competitive battlecards, anything an agent is about to ship with a source attached.

## Why this exists

Asking the model that wrote a claim how confident it is does not catch a claim it made up. This checks the page instead: does the claim's text appear on the page it cites, fetched fresh (with a Wayback Machine fallback if the live page is gone).

- **Not a similarity score.** Naive string-similarity metrics score a real quote near-zero on a long page, because they normalize by combined length. This divides only by the claim's length, so page length can't hide or manufacture a match.
- **Two signals plus a specifics check.** N-gram overlap shows quotation. Vocabulary overlap survives rewording but only shows the claim is on the same topic, so a claim that matches only on vocabulary is `low_match` and fails the run until a person reviews it. Numbers and capitalized names in the claim must appear on the page, or the claim is `unsupported`.
- **Fair to platforms that block bots.** Reddit, X, LinkedIn, Glassdoor, and Indeed 403/429 legitimate scripted fetches. That's flagged as `snippet_only`, not penalized as a dead link or a fabrication.
- **Containment, not truth.** It cannot tell you the source page itself is honest. See Limitations below and `references/methodology.md`.

## Install

Drop this directory into your project's `skills/` (or wherever your agent-skills host looks) or point your Claude Code / agent-skills-compatible host at it directly. Requires Python 3.10+, stdlib only. No dependencies to install.

## Quickstart

```bash
python3 scripts/verify_claims.py claims.json --annotate-out verified.json
```

Input: a flat JSON array:

```json
[
  {"id": "lead-1", "claim": "posted a Staff ML Engineer role three weeks ago", "source_url": "https://example.com/careers"},
  {"id": "lead-2", "claim": "raised a $12M Series A in March", "source_url": "https://example.com/news"}
]
```

Output: the same array, annotated with `verification_tier`, `verification_note`, and `verified_at` on every entry, plus a console summary. Exits 1 if anything is `unsupported` (claim or its specifics not on the page), `broken` (dead source), or `low_match` (shares vocabulary with the page but is not quoted from it, so a person has to check it).

See `SKILL.md` for the full workflow and tier meanings, `references/methodology.md` for why it's built this way.

## Limitations

- Matching is lexical. It compares words and word sequences. It cannot tell whether a reworded claim means the same thing as the page, or whether a quoted line means what your artifact says it means.
- A claim can be `verified` and still be wrong if the page is wrong, or if the page has changed since the check.
- The specifics check treats numbers and capitalized words as the facts to look for. That is an English-language shortcut. It skips the claim's first word, misses lowercase brand names, and can flag an ordinary capitalized word that the page happens not to use.
- Negation and qualifiers are invisible to it. "did not raise" and "raised" share most of their words.
- Pages that need JavaScript to render, or that block scripted fetches, can't be checked. Those come back `unverified` or `snippet_only`.

## License

MIT. See `LICENSE`.
