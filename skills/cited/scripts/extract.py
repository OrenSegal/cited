"""Turn a markdown draft into checkable (claim, source_url) pairs. Pure: no I/O.

Ported from receipts (Oren Segal, MIT), which built this front end for the
same containment check cited runs. The rest of cited needs clean
{claim, source_url} pairs; people write prose with links in it.

The rule that governs this module: a red only counts if it came from a check
that could have passed. A bare "read more [here](url)" link can never pass a
containment check, because "here" is not a claim. Reporting it as unsupported
would be a broken check, not a finding. So the extractor's job is less finding
claims than refusing to check things that cannot pass, and saying so.

Three buckets, all deterministic, no model involved:

    checkable   a declarative sentence that asserts something, with a source
    repaired    trailing-citation pattern: the claim is the previous sentence
    excluded    navigational, bare or bibliographic link: reported, never checked
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Literal

Bucket = Literal["checkable", "repaired", "excluded"]

# Link text that is navigation, not assertion. Checking these is the bug.
NAVIGATIONAL = frozenset({
    "here", "this", "that", "link", "source", "sources", "docs", "doc",
    "documentation", "read more", "more", "learn more", "see more", "details",
    "see details", "click here", "read", "reference", "references", "ref",
    "via", "website", "site", "page", "article", "post", "paper", "repo",
    "github", "download", "homepage", "official site", "full story",
    "continue reading", "as seen here", "see here", "found here", "link here",
})

# Headings under which links are bibliography, not in-line assertion.
BIBLIOGRAPHIC_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*(sources?|references?|further reading|see also|"
    r"bibliography|citations?|links?|footnotes?|appendix|related)\b",
    re.IGNORECASE,
)

INLINE_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*(<[^>]*>|[^\s)]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")  # any indent: fences nest in list items
BARE_MARKER = re.compile(r"^[\s\W\d]*$")  # "[1]", "(*)", a dash, "2."

# A sentence worth checking usually carries something falsifiable in it.
HAS_NUMBER = re.compile(r"\d")
HAS_QUOTE = re.compile(r"[\"“”'‘’]")
HAS_PROPER_NOUN = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-zA-Z0-9]{2,}\b")
HEDGE = re.compile(
    r"\b(may|might|could|would|should|perhaps|possibly|likely|probably|"
    r"i think|we think|in my (?:view|opinion)|arguably|seems?|appears?)\b",
    re.IGNORECASE,
)

# Sentence splitter that does not detonate on "U.S.", "40.5%", "e.g." or URLs.
_ABBREV = r"(?<!\b[A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)(?<!\bMr)(?<!\bMs)(?<!\bDr)(?<!\bSt)(?<!\bU\.S)(?<!\bNo)"
_SENT_SPLIT = re.compile(_ABBREV + r"(?<=[.!?])[\"'”’)\]]*\s+(?=[*_>]{0,2}[A-Z\"“'‘\[(])")


@dataclass
class Pair:
    """One link, classified, with the claim it is being asked to support."""

    id: str
    claim: str
    source_url: str
    bucket: Bucket
    link_text: str
    line: int
    reason: str
    salience: int = 0
    tags: list[str] = field(default_factory=list)

    def to_verifier_input(self) -> dict:
        return {"id": self.id, "claim": self.claim, "source_url": self.source_url}


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def _strip_markdown(text: str) -> str:
    """Flatten inline markup so the claim reads as the sentence a human read."""
    text = INLINE_LINK.sub(lambda m: m.group(1), text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", text)
    text = re.sub(r"^\s*>\s?", "", text)
    return " ".join(text.split()).strip()


def _salience(claim: str) -> tuple[int, list[str]]:
    """How falsifiable does this sentence look? Drives ordering, not filtering."""
    score, tags = 0, []
    if HAS_NUMBER.search(claim):
        score += 3
        tags.append("number")
    if HAS_QUOTE.search(claim):
        score += 2
        tags.append("quotation")
    if HAS_PROPER_NOUN.search(claim):
        score += 1
        tags.append("proper-noun")
    if HEDGE.search(claim):
        score -= 2
        tags.append("hedged")
    return score, tags


def _is_bare(link_text: str) -> bool:
    t = link_text.strip().lower().strip(".,;:!?()[]`\"'")
    t = re.sub(r"^(the|a|an|our|their|its|this|these|those|full|complete|original)\s+", "", t)
    t = re.sub(r"\s+(page|link|post|article|here|below|above)$", "", t)
    return not t or t in NAVIGATIONAL or bool(BARE_MARKER.match(link_text.strip()))


def _blocks(markdown: str) -> Iterator[tuple[int, str, bool]]:
    """Yield (1-based start line, paragraph text, under_bibliography)."""
    lines = markdown.splitlines()
    fence: tuple[str, int] | None = None  # (fence char, run length) of the open block
    biblio = False
    buf: list[str] = []
    start = 1

    def flush():
        nonlocal buf, start
        if buf:
            yielded = (start, "\n".join(buf), biblio)
            buf = []
            return yielded
        return None

    for idx, raw in enumerate(lines, start=1):
        # CommonMark: a block closes only on a run of the opener's character,
        # at least as long, with nothing after it. Anything else stays code.
        m = FENCE.match(raw)
        if m:
            run = m.group(1)
            if fence is None:
                out = flush()
                if out:
                    yield out
                fence = (run[0], len(run))
                continue
            if run[0] == fence[0] and len(run) >= fence[1] and not raw[m.end():].strip():
                fence = None
                continue
        if fence is not None:
            continue
        if raw.lstrip().startswith("#"):
            out = flush()
            if out:
                yield out
            biblio = bool(BIBLIOGRAPHIC_HEADING.match(raw))
            continue
        if not raw.strip():
            out = flush()
            if out:
                yield out
            continue
        if not buf:
            start = idx
        buf.append(raw)
    out = flush()
    if out:
        yield out


CLAUSE_BREAK = re.compile(r"[;\u2014\u2013]|(?<=\w),\s+(?:and|but|which|while|whereas)\s+")
LONG_SENTENCE_WORDS = 18


def _in_parenthetical(text: str, lo: int) -> bool:
    """Is the link inside a parenthetical aside? Then that aside is its scope."""
    opener = text.rfind("(", 0, lo)
    return opener != -1 and text.find(")", opener, lo) == -1


def _clause_around(text: str, lo: int, hi: int) -> str:
    """Shrink a long span to the clause the link sits in.

    A source dropped inside a parenthetical or after a semicolon supports that
    clause, not the forty words around it. Without this, one link inherits every
    number in the sentence, including the ones belonging to a different subject
    entirely, and a claim gets marked unsourced for specifics it never made.

    Both boundaries move independently, because a parenthetical is often only
    half-visible inside a neighbour-bounded window: the "(" is there and the
    ")" belongs to the next link's span.
    """
    start, end = 0, len(text)

    # An unclosed "(" before the link opens the clause, even if its ")" is
    # outside this span.
    opener = text.rfind("(", 0, lo)
    if opener != -1 and text.find(")", opener, lo) == -1:
        start = opener + 1
        closer = text.find(")", hi)
        if closer != -1:
            end = closer

    for m in CLAUSE_BREAK.finditer(text):
        if m.end() <= lo:
            start = max(start, m.end())
        elif m.start() >= hi:
            end = min(end, m.start())
            break
    return text[start:end] if start < end else text


def _narrow(raw_sentence: str, links: list[re.Match], i: int) -> tuple[str, bool]:
    """Give link i only the span of the sentence it actually owns.

    A sentence like "Combine A (12k stars) + B (5k stars) + C" carries three
    links and three different facts. Handing the whole sentence to each source
    guarantees every one of them scores as a partial match, because three
    quarters of the words were never going to be on that page. The window runs
    from the end of the previous link to the start of the next one.
    """
    if len(links) == 1:
        whole = _strip_markdown(raw_sentence)
        if (len(whole.split()) <= LONG_SENTENCE_WORDS
                and not _in_parenthetical(raw_sentence, links[0].start())):
            return whole, False
        clause = _strip_markdown(
            _clause_around(raw_sentence, links[0].start(), links[0].end()))
        clause = re.sub(r"^[\s,;:+&|/\u2013\u2014-]+|[\s,;:+&|/\u2013\u2014-]+$", "", clause)
        if len(clause.split()) >= 4 and len(clause.split()) < len(whole.split()):
            return clause, True
        return whole, False
    lo = links[i - 1].end() if i > 0 else 0
    hi = links[i + 1].start() if i + 1 < len(links) else len(raw_sentence)
    span = raw_sentence[lo:hi]
    # The neighbour-bounded window can still be a whole sentence's worth of
    # prose. Clause-narrow it the same way a single-link sentence is narrowed.
    if (len(_strip_markdown(span).split()) > LONG_SENTENCE_WORDS
            or _in_parenthetical(span, links[i].start() - lo)):
        clause = _clause_around(span, links[i].start() - lo, links[i].end() - lo)
        if len(_strip_markdown(clause).split()) >= 4:
            span = clause
    window = _strip_markdown(span)
    # A window can open with the tail of the *previous* link's parenthetical.
    # That text belongs to the neighbour's source, not this one.
    window = re.sub(r"^\s*\([^()]*\)\s*", "", window)
    window = re.sub(r"^[\s,;:+&|/\u2013\u2014-]+|[\s,;:+&|/\u2013\u2014-]+$", "", window)
    window = re.sub(r"^(and|or|plus|with|then)\s+", "", window, flags=re.IGNORECASE)
    return window, True


def extract(markdown: str, doc_id: str = "doc") -> list[Pair]:
    """Extract and classify every inline link in a markdown document."""
    pairs: list[Pair] = []
    seq = 0

    for start_line, block, biblio in _blocks(markdown):
        raw_sentences = split_sentences(block)
        cursor = 0

        for s_idx, raw_sentence in enumerate(raw_sentences):
            # split_sentences collapsed whitespace, so find the sentence in the
            # original block by its tokens with any whitespace (newlines too)
            # between them, searching forward from the previous sentence.
            tokens = raw_sentence.split()[:8]
            found = re.compile(r"\s+".join(map(re.escape, tokens))).search(block, cursor)
            if found:
                cursor = found.end()
            line = start_line + block[: found.start() if found else 0].count("\n")
            links = list(INLINE_LINK.finditer(raw_sentence))
            for i, match in enumerate(links):
                seq += 1
                link_text = match.group(1)
                url = match.group(2).strip("<>")
                pid = f"{doc_id}-{seq:03d}"

                if not url.lower().startswith(("http://", "https://")):
                    pairs.append(Pair(pid, "", url, "excluded", link_text, line,
                                      "not an http(s) source"))
                    continue
                if biblio:
                    pairs.append(Pair(pid, "", url, "excluded", link_text, line,
                                      "under a references/sources heading: bibliography, "
                                      "not an in-line claim"))
                    continue

                claim, narrowed = _narrow(raw_sentence, links, i)
                plain = _strip_markdown(link_text) or link_text
                bare = _is_bare(link_text)
                # "The link is most of the sentence" only means *citation marker*
                # when the link text is short. When someone writes the whole
                # assertion inside the link ("[41% of teams shipped untested
                # prose](url)"), that is the most checkable case there is, not a
                # navigational one. Requiring brevity keeps the two apart.
                swallows = (bool(claim) and len(plain) >= 0.6 * len(claim)
                            and len(plain.split()) <= 4)

                if bare or swallows:
                    prev = ""
                    for back in range(s_idx - 1, -1, -1):
                        cand = _strip_markdown(raw_sentences[back])
                        if len(cand.split()) >= 4 and not _is_bare(cand):
                            prev = cand
                            break
                    if prev:
                        score, tags = _salience(prev)
                        pairs.append(Pair(pid, prev, url, "repaired", link_text, line,
                                          "trailing citation: claim taken from the "
                                          "preceding sentence", score, tags))
                    else:
                        pairs.append(Pair(pid, claim, url, "excluded", link_text, line,
                                          "navigational link with no claim attached; "
                                          "cannot pass a containment check"))
                    continue

                if len(claim.split()) < 4:
                    pairs.append(Pair(pid, claim, url, "excluded", link_text, line,
                                      "too short to contain a checkable assertion"))
                    continue

                score, tags = _salience(claim)
                if narrowed:
                    tags.append("narrowed")
                pairs.append(Pair(pid, claim, url, "checkable", link_text, line,
                                  "declarative sentence with an attached source",
                                  score, tags))

    return pairs
