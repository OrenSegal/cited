---
name: verify-before-ship
description: Before an agent ships any artifact that cites sources (a report, a lead list, a research summary, a changelog claim), re-fetch every cited source and confirm the claim is actually there — not just self-graded by the model that wrote it. Use when an agent's output includes claims attributed to a URL and those claims need to be trustworthy before a human sees them.
license: MIT
---

# Verify Before Ship

The core failure mode this defends against: an agent writes a plausible-sounding claim, attributes it to a source it read (or half-read, or invented from a search snippet), and grades its own confidence — using the same model that might have made the claim up. Self-grading cannot catch self-invention.

This skill is a mechanism, not a vertical: it takes any list of `(claim, source_url)` pairs and re-fetches each source independently, checking whether the claim is actually contained in the page — before the artifact carrying those claims ships to a human.

Read [references/methodology.md](references/methodology.md) before wiring this into your own artifact type — it explains the tiering logic and where this mechanism is honest about its limits.

## When to use this

Any time an agent is about to hand a human a document, list, or report where individual line items are attributed to a specific source (a URL) and the human is expected to trust the attribution without independently re-checking it themselves. Examples: a lead-gen report ("this company posted this job"), a research digest ("this podcast guest said this number"), a competitive battlecard ("competitor X does Y, per their pricing page"), a citation-heavy summary.

Do **not** reach for this to fact-check claims that don't cite a specific URL — it verifies containment (is the claim on that page), not truth (is the page itself honest), and it has nothing to check without a `source_url`.

## Workflow

1. Produce your artifact's claims as a flat JSON array: `[{"id": "...", "claim": "...", "source_url": "..."}, ...]`. `id` is whatever your artifact uses to key this claim back to its full record (a lead's name, a section heading — anything stable).
2. Run `python3 scripts/verify_claims.py claims.json --annotate-out verified.json`.
3. Read the tier written back onto each entry:
   - `verified` — substantially quoted from the live (or archived) page. Ship it.
   - `low_match` — supported but paraphrased, not quoted verbatim. Ship it, but consider tightening the claim to what's actually on the page.
   - `snippet_only` — the platform blocked automated fetching (Reddit, X, LinkedIn, etc.) and no archived copy exists. Your call whether the original discovery snippet is trustworthy enough to keep; disclose the unverified status if you do.
   - `unsupported` — the page loaded fine and the claim is **not on it**. This is the fabrication signal. Drop the claim or find a claim the page actually supports.
   - `broken` — the source URL doesn't resolve, even via the Wayback Machine fallback. Drop it.
4. The script exits non-zero if anything is `unsupported` or `broken` — wire that into a CI gate or a pre-ship check if your artifact pipeline has one.
5. On the shipped artifact, disclose the result: "N of M claims verified against source." That single line is what turns a report into something a skeptical reader can trust without redoing your research.

## What this does and doesn't prove

It proves a claim's text is (or isn't) present on the page it cites, checked independently of whatever model wrote the claim. It does **not** prove the page itself is honest, that a correctly-quoted line means what your artifact says it means, or that the page hasn't changed since — every checked entry is stamped `verified_at` so staleness is visible later, not hidden.

See [references/methodology.md](references/methodology.md) for the full tiering rules, the Wayback Machine fallback behavior, and how bot-walled platforms (Reddit, X, LinkedIn) are handled without either failing the run or pretending they were checked.
