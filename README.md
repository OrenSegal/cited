# Verify Before Ship

[![CI](https://github.com/OrenSegal/verify-before-ship/actions/workflows/ci.yml/badge.svg)](https://github.com/OrenSegal/verify-before-ship/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-black)](LICENSE)

A Claude Code / agent-skills skill that catches AI-fabricated claims before they reach a human. It re-fetches every source an agent cites and confirms the claim is actually on the page, instead of trusting the same model's self-graded confidence.

Extracted and generalized from [signal-scout](https://github.com/OrenSegal/signal-scout)'s source-verification mechanism (`verify_sources.py`). Same containment-checking engine, decoupled from that project's lead-gen-specific schema so it works on any `(claim, source_url)` pair: leads, research citations, podcast quotes, changelog claims, competitive battlecards, anything an agent is about to ship with a source attached.

## Why this exists

Self-grading a claim's confidence with the same model that might have invented the claim doesn't catch invention. It just launders it with a confidence score. This skill checks containment independently: does the claim's actual text appear on the actual page, fetched fresh, right now (with a Wayback Machine fallback if the live page is gone).

- **Not a similarity score.** Naive string-similarity metrics score a real quote near-zero on a long page, because they normalize by combined length. This divides only by the claim's length, so page length can't hide or manufacture a match.
- **Two independent signals.** N-gram overlap proves quotation; distinctive-vocabulary overlap survives paraphrasing but only proves topicality. A claim needs to clear one to count as supported at all.
- **Fair to platforms that block bots.** Reddit, X, LinkedIn, Glassdoor, and Indeed 403/429 legitimate scripted fetches. That's flagged as `snippet_only`, not penalized as a dead link or a fabrication.
- **Honest about its limits.** It proves containment, not truth. It cannot tell you the source page itself is honest. See `references/methodology.md`.

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

Output: the same array, annotated with `verification_tier`, `verification_note`, and `verified_at` on every entry, plus a console summary. Exits non-zero if anything is `unsupported` (claim not on the page, the fabrication signal) or `broken` (dead source).

See `SKILL.md` for the full workflow and tier meanings, `references/methodology.md` for why it's built this way.

## Support this project

If this saved you a hallucinated-source incident, [sponsoring on GitHub](https://github.com/sponsors/OrenSegal) keeps it maintained.

## License

MIT. See `LICENSE`.
