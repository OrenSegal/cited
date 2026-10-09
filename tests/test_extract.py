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

    def test_a_fence_closes_only_on_a_matching_fence(self):
        # A shorter run or the other fence character is code, not a closer.
        for opener, inner in (("````", "```"), ("```", "~~~"), ("```", "``` not a closer")):
            md = f"{opener}\n{inner}\n[fake](https://example.com/nope)\n{opener}\n"
            self.assertEqual(extract(md, "t"), [], (opener, inner))

    def test_prose_after_a_matched_close_is_extracted(self):
        md = "````\n```\n````\n\nThe [2024 report](https://example.com/r) found 40% growth.\n"
        self.assertEqual([p.link_text for p in extract(md, "t")], ["2024 report"])


class LineNumbers(unittest.TestCase):
    """Shipped bug: a sentence that wrapped inside its first 40 characters got its block's line."""

    def test_wrapped_second_sentence_gets_its_own_line(self):
        md = ("Intro line one.\nThe Acme\nsurvey found that [40% of teams\nship weekly]"
              "(https://example.com/s).\n")
        (pair,) = extract(md, "t")
        self.assertEqual(pair.line, 2)

    def test_repeated_sentence_start_is_found_in_order(self):
        md = ("Acme said in its annual filing that revenue [grew 10%](https://example.com/a).\n"
              "Acme said in its annual filing that revenue [grew 20%](https://example.com/b).\n")
        self.assertEqual([p.line for p in extract(md, "t")], [1, 2])


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


class BareUrls(unittest.TestCase):
    """Research notes cite with bare URLs, not [text](url). Found on a real run:
    two of the owner's notes, 21 and 8 sources, extracted to zero pairs."""

    def pairs(self, md):
        return [(p.bucket, p.claim, p.source_url) for p in extract(md, "t")]

    def test_table_row_claim_comes_from_its_prose_cell(self):
        md = ("| Fact | Grade | Source |\n|---|---|---|\n"
              "| Operator is Example Holdings, Inc. | [V] | https://example.com/legal |\n"
              "| Funding: Series C $1B at $10B | [S] | https://a.example/x , https://b.example/y |\n")
        self.assertEqual(self.pairs(md), [
            ("checkable", "Operator is Example Holdings, Inc.", "https://example.com/legal"),
            ("checkable", "Funding: Series C $1B at $10B", "https://a.example/x"),
            ("checkable", "Funding: Series C $1B at $10B", "https://b.example/y"),
        ])

    def test_bracketed_citation_is_cut_from_the_claim(self):
        md = "- The files site shows only a heading without auth [V: https://files.example.com].\n"
        self.assertEqual(self.pairs(md), [
            ("checkable", "The files site shows only a heading without auth.", "https://files.example.com")])

    def test_scheme_less_reference_in_parentheses_is_inferred_and_labelled(self):
        md = "Luma Featured lifts registrations up to 5x (help.example.com/p/featuring).\n"
        (pair,) = extract(md, "t")
        self.assertEqual(pair.source_url, "https://help.example.com/p/featuring")
        self.assertIn("scheme-inferred", pair.tags)
        self.assertIn("https:// assumed", pair.reason)
        self.assertEqual(pair.claim, "Luma Featured lifts registrations up to 5x.")

    def test_bare_hostnames_and_file_paths_are_not_sources(self):
        md = "The web workspace at app.example.com reads `docs/setup.md` and (src/app/main.py) on start.\n"
        self.assertEqual(extract(md, "t"), [])

    def test_sources_list_is_bibliography(self):
        md = "## Sources\n- https://example.com/a\n- https://example.com/b (a clone tutorial)\n"
        self.assertEqual([p.bucket for p in extract(md, "t")], ["excluded", "excluded"])

    def test_url_alone_after_a_claim_repairs_from_the_previous_sentence(self):
        md = "Revenue grew 40% in 2025 at Example Corp. Source: <https://example.com/report>\n"
        self.assertEqual(self.pairs(md), [
            ("repaired", "Revenue grew 40% in 2025 at Example Corp.", "https://example.com/report")])

    def test_list_item_that_is_only_a_url_is_excluded(self):
        md = "- https://example.com/a\n"
        self.assertEqual([p.bucket for p in extract(md, "t")], ["excluded"])

    def test_inline_links_and_code_are_not_counted_twice(self):
        md = ("The [2024 report](https://example.com/r) found 40% growth.\n\n"
              "Run `curl https://example.com/api` to see the 2025 numbers.\n")
        self.assertEqual([p.source_url for p in extract(md, "t")], ["https://example.com/r"])

    def test_each_list_item_is_its_own_claim(self):
        md = ("- Venue minimums are $1-4k in the city (venues.example/blog/minimums).\n"
              "- Draft advisory allows flat fees only (sla.example.gov/advisory, marked DRAFT).\n")
        self.assertEqual([p.claim for p in extract(md, "t")], [
            "Venue minimums are $1-4k in the city.", "Draft advisory allows flat fees only."])


class BareUrlClauses(unittest.TestCase):
    """Found on a real 113-claim run: a sentence or cell packing several
    sourced facts was checked whole against each source, so every source
    failed on the others' specifics. Each claim is now the clause nearest
    its citation."""

    def claims(self, md):
        return [(p.claim, p.source_url) for p in extract(md, "t") if p.bucket != "excluded"]

    def test_semicolon_clauses_go_to_their_own_sources(self):
        md = ('- **Directories:** OpenAI bans undisclosed "behavioral profiling" and scraping [V] https://a.example/o ; '
              'Muse bans "extract or reconstruct data" and inferring attributes (4.4(iv)) [V] https://b.example/m ; '
              'Anthropic\'s policy 1.C requires protecting "the privacy interests" [V] https://c.example/p . '
              "Double-check that [U].\n")
        self.assertEqual(self.claims(md), [
            ('Directories: OpenAI bans undisclosed "behavioral profiling" and scraping', "https://a.example/o"),
            ('Muse bans "extract or reconstruct data" and inferring attributes (4.4(iv))', "https://b.example/m"),
            ('Anthropic\'s policy 1.C requires protecting "the privacy interests".', "https://c.example/p"),
        ])

    def test_sentences_after_bold_lead_ins_are_split(self):
        md = ("3. **Both stores ban the $299 report.** Lemon Squeezy prohibits services of any kind "
              "[V] https://a.example/l . Gumroad prohibits consulting firms and mailing lists [V] https://b.example/g .\n")
        self.assertEqual(self.claims(md), [
            ("Lemon Squeezy prohibits services of any kind.", "https://a.example/l"),
            ("Gumroad prohibits consulting firms and mailing lists.", "https://b.example/g"),
        ])

    def test_a_lone_citation_keeps_its_whole_sentence(self):
        md = "Shovels has 500 free credits a month, then $599/mo (https://a.example/s), which is $0.024 a record.\n"
        self.assertEqual(self.claims(md), [
            ("Shovels has 500 free credits a month, then $599/mo, which is $0.024 a record.", "https://a.example/s")])

    def test_a_short_lead_in_runs_on_past_its_citation(self):
        md = ('The plugin guidelines (https://a.example/g) say plugins "must not display subscription plans".\n')
        self.assertEqual(self.claims(md), [
            ('The plugin guidelines say plugins "must not display subscription plans".', "https://a.example/g")])

    def test_back_to_back_citations_share_a_claim(self):
        md = "- No public vendor intake form was found [V] https://a.example/m , https://b.example/r\n"
        self.assertEqual([c for c, _ in self.claims(md)], ["No public vendor intake form was found"] * 2)

    def test_table_cell_clauses_are_scoped(self):
        md = ("| Venue | Fit | Notes |\n|---|---|---|\n"
              "| Apify | 4 | creator gets 80 percent of fees [V] https://a.example/t ; "
              "payouts start from $20 via PayPal [V] https://b.example/p | Skip |\n")
        self.assertEqual(self.claims(md), [
            ("creator gets 80 percent of fees", "https://a.example/t"),
            ("payouts start from $20 via PayPal", "https://b.example/p"),
        ])

    def test_a_url_in_a_label_cell_takes_the_rows_prose(self):
        md = ("| Product | Price | Relevance |\n|---|---|---|\n"
              "| OpeningSignal (https://a.example/pricing) | $50/mo for 30 ZIPs | Closest rival |\n")
        self.assertEqual(self.claims(md), [("$50/mo for 30 ZIPs", "https://a.example/pricing")])

    def test_a_list_of_dated_example_urls_is_not_a_claim(self):
        md = "- **Example URLs:** https://a.example/1 (2017-04-19); https://b.example/2 (2023-07-10).\n"
        self.assertEqual([p.bucket for p in extract(md, "t")], ["excluded", "excluded"])
