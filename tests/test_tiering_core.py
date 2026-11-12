import pytest

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


# ── Numbers must sit near the claim's own words ─────────────────────────────

ACQUISITION_PAGE = (
    "Acme Robotics was founded in 2019 by Jane Doe. Beta Corp acquired Acme Robotics in 2022 for $15 million. "
    "Revenue fell 12 percent in 2023 while headcount grew by 40 percent. "
) * 2


def test_year_borrowed_from_elsewhere_on_the_page_needs_review():
    tier, note, _, _ = tier_for_claim("Acme Robotics was founded in 2022 by Jane Doe", ACQUISITION_PAGE)
    assert tier == TIER_LOW_MATCH
    assert "2022" in note and "human review" in note


def test_terse_claim_cannot_lean_on_the_page_subject_for_context():
    # "Acme Robotics" sits next to 2022 on the page, but it is what the whole
    # page is about, so it says nothing about what happened in 2022.
    # Its words are not in one passage either, so it is not treated as quoted at all.
    tier, note, _, _ = tier_for_claim("Acme Robotics founded in 2022", ACQUISITION_PAGE)
    assert tier == TIER_LOW_MATCH


def test_swapped_percentages_need_review():
    tier, note, _, _ = tier_for_claim(
        "Revenue fell 40 percent in 2023 while headcount grew by 12 percent", ACQUISITION_PAGE)
    assert tier == TIER_LOW_MATCH
    assert "40" in note and "12" in note


def test_verbatim_quote_with_numbers_stays_verified():
    for claim in ("Acme Robotics was founded in 2019 by Jane Doe",
                  "Beta Corp acquired Acme Robotics in 2022 for $15 million",
                  "Revenue fell 12 percent in 2023 while headcount grew by 40 percent"):
        tier, note, _, _ = tier_for_claim(claim, ACQUISITION_PAGE)
        assert tier == TIER_VERIFIED, note


def test_scale_and_currency_words_are_not_context():
    tier, note, _, _ = tier_for_claim(
        "Beta Corp acquired Acme Robotics in 2022 for 15 million dollars", ACQUISITION_PAGE)
    assert tier == TIER_VERIFIED, note


def test_number_with_no_distinctive_neighbors_is_not_flagged():
    page = "It was 2022 when the team shipped the first version of the product to customers. " * 5
    tier, note, _, _ = tier_for_claim("It was 2022 when the team shipped the first version", page)
    assert tier == TIER_VERIFIED, note


def test_number_not_on_the_page_at_all_is_still_unsupported():
    tier, note, _, _ = tier_for_claim("Acme Robotics was founded in 2021 by Jane Doe", ACQUISITION_PAGE)
    assert tier == TIER_UNSUPPORTED
    assert "2021" in note


# ── Formatting and sentence position are not specifics ──────────────────────


def test_trailing_zero_decimal_matches_the_whole_number():
    tier, note, _, _ = tier_for_claim("Beta Corp acquired Acme Robotics in 2022 for $15.0 million", ACQUISITION_PAGE)
    assert tier == TIER_VERIFIED, note


def test_a_real_decimal_is_still_a_different_number():
    tier, note, _, _ = tier_for_claim("Beta Corp acquired Acme Robotics in 2022 for $15.5 million", ACQUISITION_PAGE)
    assert tier == TIER_UNSUPPORTED
    assert "15.5" in note


def test_sentence_initial_word_is_not_a_name():
    tier, note, _, _ = tier_for_claim(
        "Beta Corp acquired Acme Robotics in 2022. Moreover Jane Doe founded it.", ACQUISITION_PAGE)
    assert tier != TIER_UNSUPPORTED, note
    assert "Moreover" not in note


def test_name_after_an_abbreviation_or_mid_sentence_is_still_a_name():
    for claim in ("Beta Corp acquired Acme Robotics from Dr. Sequoia", "Beta Corp acquired Acme Robotics from Sequoia"):
        tier, note, _, _ = tier_for_claim(claim, ACQUISITION_PAGE)
        assert tier == TIER_UNSUPPORTED
        assert "Sequoia" in note


def test_invented_name_opening_a_later_sentence_is_flagged():
    from tiering_core import missing_specifics

    assert "Sequoia" in missing_specifics(
        "Beta Corp acquired Acme Robotics in 2022. Sequoia led the round.", ACQUISITION_PAGE)


# ── Wrong verdicts found by attacking the checker ───────────────────────────

REST_OF_PAGE = (" The quarterly letter to shareholders also covers hiring, office moves, the product roadmap for the "
                "coming year, notes on governance, a summary of the annual meeting and the dividend schedule.")


def _tier(claim, page):
    return tier_for_claim(claim, page + REST_OF_PAGE)[0]


def test_footnote_markers_are_not_read_as_one_number():
    assert _tier("The team hired 12 engineers in the third quarter.",
                 "The team hired engineers in the third quarter (see notes 1,2).") == TIER_UNSUPPORTED
    assert _tier("Revenue grew 15% in the third quarter.", "Revenue grew 1,5% in the third quarter.") == TIER_UNSUPPORTED
    assert _tier("The fund manages 1,250,000 accounts for savers.",
                 "The fund manages 1250000 accounts for savers.") != TIER_UNSUPPORTED


def test_another_currency_is_not_the_same_amount():
    assert _tier("Acme raised \u20ac40 million in new funding from investors.",
                 "Acme raised $40 million in new funding from investors.") == TIER_UNSUPPORTED
    assert _tier("Acme raised $40 million in new funding.", "Acme raised 40 million dollars in new funding.") == TIER_VERIFIED
    assert _tier("Acme raised $40M in new funding.", "Acme raised US$40m in new funding.") == TIER_VERIFIED


def test_a_dropped_minus_sign_is_not_the_same_number():
    assert _tier("Operating margin was 5% for the full fiscal year.",
                 "Operating margin was -5% for the full fiscal year.") == TIER_UNSUPPORTED
    assert _tier("Revenue rose every year in 2019-2024 at the firm.",
                 "Revenue rose every year in 2019-2024 at the firm.") == TIER_VERIFIED


def test_claim_stitched_from_two_sentences_needs_review():
    page = ("Alice Chen was hired as chief executive in January. "
            "Later, Bob Diaz was fired for fraud by the board in March.")
    tier, note, _, _ = tier_for_claim("Alice Chen was fired for fraud by the board in March.", page + REST_OF_PAGE)
    assert tier == TIER_LOW_MATCH and "Chen" in note
    assert _tier("Bob Diaz was fired for fraud by the board in March.", page) == TIER_VERIFIED


def test_phrases_from_distant_parts_of_a_page_are_not_a_quote():
    page = ("The board met in March to review the budget." + REST_OF_PAGE * 3
            + " Staff were fired for fraud at a supplier last year.")
    assert _tier("The board fired staff in March for fraud at a supplier.", page) == TIER_LOW_MATCH


@pytest.mark.xfail(strict=True, reason="word order inside one passage is not checked; see README Limitations")
def test_swapped_subject_and_object_is_not_verified():
    assert _tier("Globex acquired Initech for $5 billion in cash and stock in 2020.",
                 "Initech acquired Globex for $5 billion in cash and stock in 2020.") != TIER_VERIFIED


def test_possessive_form_of_a_name_on_the_page_is_the_same_name():
    assert _tier("The round was led by Sequoia's growth fund this spring.",
                 "The round was led by Sequoia and its growth fund this spring.") == TIER_VERIFIED
    assert _tier("The round was led by Benchmark's growth fund this spring.",
                 "The round was led by Sequoia and its growth fund this spring.") == TIER_UNSUPPORTED


def test_number_spellings_that_mean_the_same():
    assert _tier("Revenue grew 15% in the third quarter.", "Revenue grew 15 per cent in the third quarter.") == TIER_VERIFIED
    assert _tier("The rate was cut by 0.5 points this month.", "The rate was cut by 0.50 points this month.") == TIER_VERIFIED
    assert _tier("The rate was cut by 0.5% this month by the bank.", "The rate was cut by .5% this month by the bank.") == TIER_VERIFIED
