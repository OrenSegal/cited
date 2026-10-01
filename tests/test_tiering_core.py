import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "skills" / "cited" / "scripts"))

from tiering_core import (
    TIER_BROKEN,
    TIER_DISQUALIFYING,
    TIER_LOW_MATCH,
    TIER_SNIPPET_ONLY,
    TIER_UNSUPPORTED,
    TIER_UNVERIFIED,
    TIER_VERIFIED,
    claim_signals,
    tier_for_claim,
)

LONG_PAGE = "We are hiring for a Staff ML Engineer role. " * 20


def test_verbatim_quote_scores_high_even_on_a_long_page():
    quoted, _ = claim_signals("hiring for a Staff ML Engineer role", LONG_PAGE)
    assert quoted > 0.5


def test_tier_for_claim_verified_on_exact_quote():
    tier, note, quoted, topical = tier_for_claim(
        "hiring for a Staff ML Engineer role", LONG_PAGE
    )
    assert tier == TIER_VERIFIED
    assert note == ""


def test_tier_for_claim_unsupported_when_claim_absent():
    tier, note, quoted, topical = tier_for_claim(
        "raised a $50M Series B in January", LONG_PAGE
    )
    assert tier == TIER_UNSUPPORTED
    assert note


ACME_PAGE = (
    "Acme Robotics builds warehouse automation software for mid-size logistics "
    "companies. Our engineering team is based in Austin and we are growing. "
) * 4


def test_fabricated_funding_claim_on_page_that_only_names_the_company_is_unsupported():
    # Regression: this used to score low_match (topical 0.4 from "acme" and
    # "robotics" alone), and low_match was documented as shippable.
    tier, note, quoted, topical = tier_for_claim(
        "Acme Robotics raised a $12M Series A led by Sequoia", ACME_PAGE
    )
    assert tier == TIER_UNSUPPORTED
    assert "Sequoia" in note and "12" in note


def test_wrong_number_in_otherwise_quoted_claim_is_unsupported():
    page = "We raised a $15M Series A led by Sequoia last spring. " * 10
    tier, note, _, _ = tier_for_claim("raised a $12M Series A led by Sequoia", page)
    assert tier == TIER_UNSUPPORTED
    assert "12" in note


def test_number_matches_across_formatting():
    page = "We raised a $12 million Series A led by Sequoia last spring. " * 10
    tier, _, _, _ = tier_for_claim("raised a $12M Series A led by Sequoia", page)
    assert tier == TIER_VERIFIED


def test_wrong_scale_word_is_unsupported():
    page = "We raised a $40 million Series B led by Northwind last spring. " * 10
    tier, note, _, _ = tier_for_claim("raised a $40 billion Series B led by Northwind", page)
    assert tier == TIER_UNSUPPORTED
    assert "40 billion" in note


def test_scale_matches_across_abbreviation():
    page = "We raised a $40M Series B led by Northwind last spring. " * 10
    tier, note, _, _ = tier_for_claim("raised a $40 million Series B led by Northwind", page)
    assert tier != TIER_UNSUPPORTED, note


def test_wrong_single_letter_identifier_is_unsupported():
    page = "We raised a $40 million Series B led by Northwind last spring. " * 10
    tier, note, _, _ = tier_for_claim("raised a $40 million Series C led by Northwind", page)
    assert tier == TIER_UNSUPPORTED
    assert "Series C" in note


def test_tier_for_claim_low_match_on_paraphrase_with_specifics_present():
    tier, note, quoted, topical = tier_for_claim(
        "Acme is adding engineering staff in Austin", ACME_PAGE
    )
    assert tier == TIER_LOW_MATCH
    assert "human review" in note
    assert quoted < 0.55


def test_paraphrase_with_a_name_missing_from_the_page_is_unsupported():
    page = "The company announced a new engineering leadership hire this quarter, " * 15
    tier, note, _, _ = tier_for_claim("hired a new VP of Engineering", page)
    assert tier == TIER_UNSUPPORTED
    assert "VP" in note


def test_tier_for_claim_unverified_on_thin_page():
    tier, note, quoted, topical = tier_for_claim("anything", "too short")
    assert tier == TIER_UNVERIFIED
    assert quoted == 0.0 and topical == 0.0


def test_disqualifying_tiers_exclude_verified_and_low_match():
    assert TIER_BROKEN in TIER_DISQUALIFYING
    assert TIER_UNSUPPORTED in TIER_DISQUALIFYING
    assert TIER_VERIFIED not in TIER_DISQUALIFYING
    assert TIER_LOW_MATCH not in TIER_DISQUALIFYING


def test_low_match_needs_review_and_blocks_shipping():
    from tiering_core import TIER_NEEDS_REVIEW, blocking_reason

    assert TIER_LOW_MATCH in TIER_NEEDS_REVIEW
    assert TIER_VERIFIED not in TIER_NEEDS_REVIEW
    assert blocking_reason({TIER_VERIFIED: 3, TIER_LOW_MATCH: 1}) == "needs_review"
    assert blocking_reason({TIER_LOW_MATCH: 1, TIER_UNSUPPORTED: 1}) == "disqualifying"
    assert blocking_reason({TIER_BROKEN: 1}) == "disqualifying"
    assert blocking_reason({TIER_VERIFIED: 3, TIER_SNIPPET_ONLY: 1}) is None
    assert blocking_reason({}) is None


def test_claim_signals_empty_inputs_return_zero():
    assert claim_signals("", LONG_PAGE) == (0.0, 0.0)
    assert claim_signals("something", "") == (0.0, 0.0)


def test_letter_identifier_needs_whole_word_on_page():
    page = "The fund issued Series Bonds to Northwind investors last spring. " * 10
    tier, note, _, _ = tier_for_claim("The fund issued Series B to Northwind investors", page)
    assert tier == TIER_UNSUPPORTED
    assert "Series B" in note


def test_pronoun_i_is_not_an_identifier():
    page = "Yesterday we shipped the new release to every Northwind customer. " * 10
    tier, note, _, _ = tier_for_claim("Yesterday I shipped the new release to every Northwind customer", page)
    assert tier != TIER_UNSUPPORTED, note
