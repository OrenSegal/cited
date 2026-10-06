"""What the markdown extractor must never get wrong.

Ported from receipts with the extractor. Every case came from running it on a
real document and finding it wrong: each is a bug that shipped, was caught by
a falsification run, and must not come back.
"""

import unittest

from conftest import FIXTURES
from extract import extract


def buckets(md):
    return {p.link_text: (p.bucket, p.claim) for p in extract(md, "t")}


class NavigationalLinks(unittest.TestCase):
    """A link with no claim can never pass containment, so it must never fail it."""

    def test_bare_here_is_not_a_claim(self):
        b = buckets("Read more [here](https://example.com/x) for background.")
        self.assertEqual(b["here"][0], "excluded")

    def test_article_prefixed_navigation_is_caught(self):
        # Shipped bug: "the docs" missed the stoplist because of the article.
        b = buckets("See [the docs](https://example.com/d) for setup details.")
        self.assertEqual(b["the docs"][0], "excluded")

    def test_relative_paths_are_not_sources(self):
        b = buckets("Check [`settings.json`](./settings.json) first.")
        self.assertEqual(b["`settings.json`"][0], "excluded")

    def test_bibliography_is_not_inline_citation(self):
        md = "## Sources\n\n- [Annual report](https://example.com/a)\n"
        self.assertEqual(buckets(md)["Annual report"][0], "excluded")

    def test_links_inside_code_fences_are_ignored(self):
        md = "```\n[fake](https://example.com/nope)\n```\n"
        self.assertEqual(extract(md, "t"), [])


class ClaimsInsideLinkText(unittest.TestCase):
    """Shipped bug: a long claim written *inside* the link was skipped as navigational."""

    def test_long_link_text_is_the_claim(self):
        md = ("Litmus states that [a green only ever comes from a check that "
              "could have failed](https://example.com/l).")
        (bucket, claim), = buckets(md).values()
        self.assertEqual(bucket, "checkable")
        self.assertIn("could have failed", claim)


class TrailingCitations(unittest.TestCase):
    def test_claim_is_recovered_from_previous_sentence(self):
        md = "Revenue grew 38% year over year. [Source](https://example.com/ir)"
        bucket, claim = buckets(md)["Source"]
        self.assertEqual(bucket, "repaired")
        self.assertIn("38%", claim)

    def test_trailing_citation_with_nothing_before_it_is_excluded(self):
        self.assertEqual(buckets("[Source](https://example.com/ir)")["Source"][0], "excluded")


class ClaimScoping(unittest.TestCase):
    """One link must not inherit the whole sentence's facts: that manufactures false reds."""

    def test_each_link_owns_its_own_span(self):
        md = ("We combined [Alpha](https://example.com/a) (12,000 stars) and "
              "[Beta](https://example.com/b) (900 stars) into one pipeline.")
        b = buckets(md)
        self.assertIn("12,000", b["Alpha"][1])
        self.assertNotIn("12,000", b["Beta"][1])
        self.assertIn("900", b["Beta"][1])

    def test_parenthetical_citation_is_scoped_to_its_aside(self):
        # Shipped bug: this claim was marked a fabrication because it inherited
        # "16.8k stars" and "NVIDIA", which belong to the subject *outside* the
        # parenthesis, not to the repo cited inside it.
        md = ("It's NVIDIA-backed with 16.8k stars and an ecosystem forming around it "
              "([skillspector-quality](https://example.com/q) layers a quality score).")
        claim = buckets(md)["skillspector-quality"][1]
        self.assertNotIn("NVIDIA", claim)
        self.assertIn("quality score", claim)


class PatternsFixture(unittest.TestCase):
    """The receipts patterns fixture, end to end through the extractor."""

    def test_every_link_lands_in_the_expected_bucket(self):
        pairs = extract((FIXTURES / "draft_patterns.md").read_text(encoding="utf-8"), "p")
        got = [(p.line, p.bucket) for p in pairs]
        self.assertEqual(got, [
            (3, "checkable"), (6, "repaired"), (8, "excluded"), (10, "excluded"),
            (12, "checkable"), (12, "checkable"), (15, "checkable"), (18, "checkable"),
            (21, "excluded"), (29, "excluded"), (29, "excluded"),
        ])

    def test_no_pair_is_checked_without_a_claim(self):
        pairs = extract((FIXTURES / "draft_patterns.md").read_text(encoding="utf-8"), "p")
        for p in pairs:
            if p.bucket != "excluded":
                self.assertGreaterEqual(len(p.claim.split()), 4, p)
