# Methodology

This is the reference material an agent (or a human wiring this into a pipeline) should read before trusting the tiering output — the reasoning, not just the mechanism, per the skill's own workflow note.

## Why containment, not similarity

The naive approach to "is this claim on the page" is a string-similarity score between the claim and the page. That's wrong on real pages: a 200-character claim and a 40,000-character page are never "similar" by any symmetric metric (ratio-of-common-subsequence, cosine similarity on the raw text, etc.) — those metrics normalize by the combined length of both inputs, so a verbatim quote sitting inside a long page scores near zero purely because the page is long. That inverts the result on exactly the pages you care about most (a company's full "About" page, a long Reddit thread, a job posting embedded in a careers page).

Containment fixes this by dividing only by the length of the **claim**, never the page. Two independent signals are computed:

- **Quoted** — n-gram (word-sequence) overlap between the claim and the page. High only when the claim's actual wording appears in sequence on the page. This is the signal that proves quotation, not just topical relevance.
- **Topical** — overlap of the claim's distinctive (non-stopword) vocabulary with the page. Survives paraphrasing, but only proves the claim is *about* what the page is about — not that the specific assertion is there.

A claim needs to clear a **quoted** threshold to be marked `verified`, a **topical** threshold (without quotation) to be marked `low_match`, and below both is `unsupported`, the fabrication signal.

Before either threshold applies, the claim's specifics are checked: every number in the claim (`$12M` and `$12 million` both reduce to `12`) and every capitalized word after the first must appear on the page. If any is missing the claim is `unsupported`, whatever its scores. Without this, "Acme Robotics raised a $12M Series A led by Sequoia" checked against Acme's homepage scored `low_match`, because "acme" and "robotics" alone cleared the topical threshold. A claim with one wrong number could also clear the quoted threshold on bigrams alone.

`low_match` is not shippable. Shared vocabulary is what a real paraphrase looks like, and it is also what a fabricated claim about a real entity looks like; this check cannot tell them apart. The script fails the run on `low_match` so a human reads the claim against the page before it ships.

## Why unsupported is worse than broken

A broken link (`404`, DNS failure, invalid URL) is a research hygiene problem — annoying, but not evidence anyone invented anything. A page that loads fine, is fully readable, and simply doesn't contain the claim attributed to it is the actual fabrication signal: the model had the real page and wrote something not on it anyway. Both fail the run (both are in `TIER_DISQUALIFYING`), but treat `unsupported` results as the higher-priority thing to investigate — it's the one that indicates invention, not just staleness.

## Why platform 403s don't fail the run

Several major platforms (Reddit's new UI, X/Twitter, LinkedIn, Glassdoor, Indeed) return `403`/`429` to any scripted fetch, including completely legitimate, live pages — this is an anti-bot wall, not evidence the page is dead or the claim is fabricated. Treating it as `broken` would fail real, honest runs purely because the source happens to live on a platform that blocks bots. Those cases fall through to `snippet_only`: not verified, but explicitly *not* penalized as if it were a dead link or a fabrication — the distinction is disclosed in the note, and it's a judgment call left to whoever is shipping the artifact whether the original discovery snippet is trustworthy enough to keep.

## Why the Wayback Machine fallback exists

A page can go down, get paywalled, or get restructured between when a claim was researched and when it's verified. Before giving up and marking a source `broken`, the script checks the Wayback Machine for an archived copy close to the original fetch — a claim verified against an honest archived snapshot is still a claim verified against the page as it was published, which is strictly better evidence than nothing, and is disclosed as such in the verification note rather than silently presented as a live-page check.

## What this cannot do

It cannot tell you the source page itself is honest — a scam page can host a false claim just as easily as a true one, and containment-checking will happily mark a false-but-actually-quoted claim as `verified`. It cannot tell you a correctly quoted line means what your artifact's surrounding text implies it means (context can be stripped in quotation). And a `verified_at` timestamp is exactly that — a point-in-time check, not a guarantee the page hasn't changed since. Disclose the limitation on anything you ship; don't oversell the tier as more than it is.
