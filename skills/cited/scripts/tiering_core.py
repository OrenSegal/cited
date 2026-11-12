"""Scores a claim against the text of the page it cites and assigns a tier. Pure: no I/O."""

from __future__ import annotations

import re

TIER_VERIFIED = "verified"          # source fetched, claim is substantially quoted from the live page
TIER_SNIPPET_ONLY = "snippet_only"  # source blocked automated fetch; claim is snippet-sourced only
TIER_LOW_MATCH = "low_match"        # source fetched, claim shares vocabulary but is not quoted; needs human review
TIER_UNSUPPORTED = "unsupported"    # source fetched and readable, but the claim is not on the page at all
TIER_UNVERIFIED = "unverified"      # not checked, or page yielded no extractable text to check against
TIER_BROKEN = "broken"              # source unreachable or invalid — should not reach a shipped artifact

TIER_LABELS = {
    TIER_VERIFIED: "Verified",
    TIER_SNIPPET_ONLY: "Snippet-only",
    TIER_LOW_MATCH: "Needs review",
    TIER_UNSUPPORTED: "Not on page",
    TIER_UNVERIFIED: "Unverified",
    TIER_BROKEN: "Broken source",
}

# Tiers that must never reach a shipped artifact or downstream handoff.
# TIER_UNSUPPORTED is the fabrication signal — the source loaded fine and the
# claim simply isn't in it, which is strictly worse than a dead link.
TIER_DISQUALIFYING = frozenset({TIER_BROKEN, TIER_UNSUPPORTED})

# Tiers that are not safe to ship without a human checking the claim against
# the page. A paraphrase match only shows the claim shares vocabulary with the
# page, which is also what a fabricated claim about a real entity looks like.
TIER_NEEDS_REVIEW = frozenset({TIER_LOW_MATCH})

# Tiers where the claim was never actually checked against page text. They do
# not block by default (the source may simply block bots or need JavaScript),
# but `strict` mode treats them as blocking.
TIER_UNCHECKED = frozenset({TIER_UNVERIFIED, TIER_SNIPPET_ONLY})

# Every tier, in severity order (most severe first). Used for stable output.
TIERS = (TIER_UNSUPPORTED, TIER_BROKEN, TIER_LOW_MATCH, TIER_UNVERIFIED, TIER_SNIPPET_ONLY, TIER_VERIFIED)


def blocking_reason(counts: dict[str, int], strict: bool = False) -> str | None:
    """Return why a run with these per-tier counts must not ship, or None.

    "disqualifying" (unsupported/broken) wins over "needs_review" (low_match),
    which wins over "unchecked" (unverified/snippet_only, strict mode only)."""
    if any(counts.get(t, 0) for t in TIER_DISQUALIFYING):
        return "disqualifying"
    if any(counts.get(t, 0) for t in TIER_NEEDS_REVIEW):
        return "needs_review"
    if strict and any(counts.get(t, 0) for t in TIER_UNCHECKED):
        return "unchecked"
    return None


def plural(n: int, singular: str, many: str | None = None) -> str:
    """ "1 claim", "2 claims": a count with its noun, for user-facing text."""
    return f"{n} {singular if n == 1 else (many or singular + 's')}"


def agree(n: int, one: str, many: str) -> str:
    """The form of a word that agrees with a count: agree(1, "was", "were")."""
    return one if n == 1 else many


def is_blocking(tier: str, strict: bool = False) -> bool:
    return tier in TIER_DISQUALIFYING or tier in TIER_NEEDS_REVIEW or (strict and tier in TIER_UNCHECKED)


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


def _normalize_number(number: str) -> str:
    """ "1,000" and "1000" are one number, as are "15.0" and "15". """
    return re.sub(r"\.0+$", "", number.replace(",", ""))


def _numbers(text: str) -> set[str]:
    return {_normalize_number(n) for n in re.findall(r"\d+(?:[.,]\d+)*", str(text or ""))}


_SCALE_WORDS = {
    "k": "thousand", "thousand": "thousand",
    "m": "million", "mn": "million", "mm": "million", "million": "million",
    "b": "billion", "bn": "billion", "billion": "billion",
    "t": "trillion", "tn": "trillion", "trillion": "trillion",
    "%": "percent", "percent": "percent",
}


def _quantities(text: str) -> set[str]:
    """Numbers carrying a scale word, normalized: "$40M" and "40 million" both
    become "40 million". A bare number check can't tell $40 million from
    $40 billion; this can."""
    found = set()
    pattern = r"(\d+(?:[.,]\d+)*)\s*(%|[A-Za-z]+)\b|(\d+(?:[.,]\d+)*)\s*(%)"
    for m in re.finditer(pattern, str(text or "")):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        scale = _SCALE_WORDS.get(unit.lower())
        if scale:
            found.add(f"{_normalize_number(num)} {scale}")
    return found


def _letter_identifiers(text: str) -> set[str]:
    """Capitalized word followed by a single capital letter: "Series B",
    "Class A". The one-letter part is the fact, and it's too short to pass
    the name check on its own."""
    return {f"{w} {letter}" for w, letter in re.findall(r"\b([A-Z][a-z]+) ([A-Z])\b", str(text or ""))
            if letter != "I"}  # the pronoun, as in "Yesterday I"


# Sentence ends, except after a title or an initial ("Dr. Lee", "J. Lee"), whose
# next word is a name and not capitalized by position.
_SENTENCE_BREAK = re.compile(
    r"(?<=[.!?])(?<!\bMr\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bMrs\.)(?<!\bProf\.)(?<!\b[A-Z]\.)"
    r"[\"')\]]*\s+(?=[\"'(\[]*[A-Z])"
)


# Ordinary words that open a later sentence. Anything else capitalized there may be a name.
_SENTENCE_OPENERS = frozenset({
    "moreover", "however", "furthermore", "additionally", "also", "meanwhile", "still", "yet",
    "then", "next", "finally", "first", "second", "third", "instead", "overall", "today",
    "recently", "later", "previously", "currently", "thus", "therefore", "similarly", "likewise",
    "nevertheless", "otherwise", "besides", "both", "many", "most", "some", "several", "after",
    "before", "since", "while", "when", "although", "because", "if", "in", "on", "at", "by",
    "for", "from", "with", "as", "to", "of",
})


def missing_specifics(claim: str, page_text: str) -> list[str]:
    """Numbers (with their scale word), capitalized names and one-letter
    identifiers like "Series B" in the claim that do not appear on the page.

    A fabricated claim about a real entity (a funding round, an investor, a
    date) shares the entity's name with the page, so it clears the topical
    signal. Its specifics are what give it away. Lexical and English-centric:
    capitalized tokens stand in for names. The claim's first token is skipped
    because it is capitalized by position, and so is a later sentence's first
    token when it is an ordinary opener.
    """
    missing = sorted(_numbers(claim) - _numbers(page_text))
    missing += sorted(_quantities(claim) - _quantities(page_text))
    page_lower = " " + " ".join(_words(page_text)) + " "
    missing += sorted(i for i in _letter_identifiers(claim)
                      if " " + " ".join(_words(i)) + " " not in page_lower)
    page_words = set(_words(page_text))
    for i, sentence in enumerate(_SENTENCE_BREAK.split(str(claim or ""))):
        tokens = re.findall(r"[A-Za-z][A-Za-z']*", sentence)
        skip_first = i == 0 or (tokens and (tokens[0].lower() in _STOPWORDS or tokens[0].lower() in _SENTENCE_OPENERS))
        for token in tokens[1:] if skip_first else tokens:
            if len(token) >= 2 and token[0].isupper() and token.lower() not in page_words:
                if token not in missing:
                    missing.append(token)
    return missing


# A word this close to a number, in the same sentence, is taken to be about it.
_NUMBER_WINDOW = 4
_UNIT_WORDS = frozenset(_SCALE_WORDS) | {"dollars", "euros", "pounds"}


def _number_windows(text: str) -> list[tuple[str, list[str]]]:
    """Each number in `text`, with the words around it in its sentence."""
    found = []
    for sentence in _SENTENCE_BREAK.split(str(text or "")):
        tokens = re.findall(r"\d+(?:[.,]\d+)*|[A-Za-z][A-Za-z']*", sentence)
        for i, token in enumerate(tokens):
            if token[0].isdigit():
                near = tokens[max(0, i - _NUMBER_WINDOW):i] + tokens[i + 1:i + 1 + _NUMBER_WINDOW]
                found.append((_normalize_number(token), [w for w in near if w[0].isalpha()]))
    return found


def _context_words(words: list[str]) -> set[str]:
    lowered = (w.lower() for w in words)
    return {w for w in lowered if len(w) > 3 and w not in _STOPWORDS and w not in _UNIT_WORDS}


def numbers_out_of_context(claim: str, page_text: str) -> list[str]:
    """Numbers in the claim that are on the page, but never next to the words
    the claim puts around them: "founded in 2022" on a page that says founded
    in 2019 and sold in 2022.

    The claim's lowercase neighbors are preferred, because a capitalized one
    is usually the page's subject and sits next to every number on the page.
    A number with no distinctive neighbors is not judged. Heuristic: this can
    only ask for a human, it never calls a claim unsupported.
    """
    page_context: dict[str, set[str]] = {}
    for number, near in _number_windows(page_text):
        page_context.setdefault(number, set()).update(w.lower() for w in near)
    out = []
    for number, near in _number_windows(claim):
        wanted = _context_words([w for w in near if w[0].islower()]) or _context_words(near)
        if wanted and not wanted & page_context.get(number, set()) and number not in out:
            out.append(number)
    return out


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
    missing = missing_specifics(claim, page_text)
    if missing:
        return (TIER_UNSUPPORTED,
                "Claim's specifics are not on the page it cites: " + ", ".join(missing),
                quoted, topical)
    if quoted >= QUOTED_THRESHOLD:
        stray = numbers_out_of_context(claim, page_text)
        if stray:
            return (TIER_LOW_MATCH,
                    "Claim's numbers are on the page but not next to the claim's words: "
                    + ", ".join(stray) + "; needs human review",
                    quoted, topical)
        return TIER_VERIFIED, "", quoted, topical
    if topical >= SUPPORTED_THRESHOLD:
        return TIER_LOW_MATCH, "Claim shares vocabulary with the page but is not quoted from it; needs human review", quoted, topical
    return TIER_UNSUPPORTED, "Claim is not found on the page it cites", quoted, topical
