"""Containment-based claim/source tiering — the reusable core of verify-before-ship.

Adapted from signal-scout's `signal_scout_core.py` (github.com/OrenSegal/signal-scout),
generalized to work on any (claim, source page text) pair instead of a specific
prospect schema. No project-specific fields — this module doesn't know or care
what domain the claim came from.
"""

from __future__ import annotations

import re

TIER_VERIFIED = "verified"          # source fetched, claim is substantially quoted from the live page
TIER_SNIPPET_ONLY = "snippet_only"  # source blocked automated fetch; claim is snippet-sourced only
TIER_LOW_MATCH = "low_match"        # source fetched, claim is a paraphrase — supported but not quoted
TIER_UNSUPPORTED = "unsupported"    # source fetched and readable, but the claim is not on the page at all
TIER_UNVERIFIED = "unverified"      # not checked, or page yielded no extractable text to check against
TIER_BROKEN = "broken"              # source unreachable or invalid — should not reach a shipped artifact

TIER_LABELS = {
    TIER_VERIFIED: "Verified",
    TIER_SNIPPET_ONLY: "Snippet-only",
    TIER_LOW_MATCH: "Paraphrased",
    TIER_UNSUPPORTED: "Not on page",
    TIER_UNVERIFIED: "Unverified",
    TIER_BROKEN: "Broken source",
}

# Tiers that must never reach a shipped artifact or downstream handoff.
# TIER_UNSUPPORTED is the fabrication signal — the source loaded fine and the
# claim simply isn't in it, which is strictly worse than a dead link.
TIER_DISQUALIFYING = frozenset({TIER_BROKEN, TIER_UNSUPPORTED})


# ── Claim/page containment ─────────────────────────────────────────────────
#
# Asks "is this claim contained in that page?", NOT "are these two strings
# similar?". The distinction matters: a 200-character quote and a 40,000-
# character page are never "similar" by any symmetric string metric — most
# similarity ratios normalize by combined length, so a verbatim quote on a
# long page scores near zero. Containment divides by the *claim* alone, so
# page length cannot move the score.

_STOPWORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "have", "has", "had", "been", "was", "were",
    "are", "its", "our", "their", "they", "them", "from", "into", "about", "would", "could",
    "should", "just", "very", "really", "some", "what", "when", "where", "which", "while",
    "than", "then", "there", "here", "will", "your", "you", "but", "not", "all", "can", "out",
})

# A quoted claim shares exact word sequences with the page; a paraphrase only
# shares vocabulary. Two signals, because they fail differently: n-grams prove
# quotation but miss rewording, rare words survive rewording but prove only
# topicality. A claim needs one of them to be considered supported at all.
QUOTED_THRESHOLD = 0.55      # >= this much n-gram overlap => substantially quoted
SUPPORTED_THRESHOLD = 0.10   # below this on *both* signals => the claim is not on the page

# Below this much extracted text we didn't really get the page (JS-rendered SPA,
# consent wall, paywall stub). That's a fetch failure, not a fabrication signal.
MIN_PAGE_TEXT_CHARS = 200


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _ngrams(words: list[str], size: int) -> set[tuple[str, ...]]:
    if len(words) < size:
        return set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def claim_signals(claim: str, page_text: str) -> tuple[float, float]:
    """Return (quoted, topical), each 0-1, measuring how much of `claim` is
    present in `page_text`.

    `quoted` is n-gram containment — the fraction of the claim's word
    sequences found on the page. High only for real quotation.
    `topical` is the fraction of the claim's distinctive (non-stopword)
    terms found on the page. Survives rewording, but proves only that the
    claim talks about what the page talks about.

    Both denominators are the claim, so neither is affected by page length.
    """
    claim_words, page_words = _words(claim), _words(page_text)
    if not claim_words or not page_words:
        return 0.0, 0.0

    def containment(size: int) -> float:
        wanted = _ngrams(claim_words, size)
        if not wanted:
            return 0.0
        return len(wanted & _ngrams(page_words, size)) / len(wanted)

    # Trigrams are the cleanest quotation proof; bigrams are discounted because
    # they collide by chance far more often on a large page.
    quoted = max(containment(3), 0.8 * containment(2))

    distinctive = {w for w in claim_words if len(w) > 3 and w not in _STOPWORDS}
    topical = len(distinctive & set(page_words)) / len(distinctive) if distinctive else 0.0
    return quoted, topical


def tier_for_claim(claim: str, page_text: str) -> tuple[str, str, float, float]:
    """Classify one claim against its fetched source page.

    Returns (tier, note, quoted_score, topical_score).
    """
    if len(page_text.strip()) < MIN_PAGE_TEXT_CHARS:
        return TIER_UNVERIFIED, "Source page yielded too little extractable text to check against", 0.0, 0.0

    quoted, topical = claim_signals(claim, page_text)
    if quoted >= QUOTED_THRESHOLD:
        return TIER_VERIFIED, "", quoted, topical
    if topical >= SUPPORTED_THRESHOLD:
        return TIER_LOW_MATCH, "Claim is supported but not verbatim-quoted from the page", quoted, topical
    return TIER_UNSUPPORTED, "Claim is not found on the page it cites", quoted, topical
