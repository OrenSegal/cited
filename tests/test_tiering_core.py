import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from tiering_core import (
    TIER_BROKEN,
    TIER_DISQUALIFYING,
    TIER_LOW_MATCH,
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


def test_tier_for_claim_low_match_on_paraphrase():
    page = "The company announced a new engineering leadership hire this quarter, " * 15
    tier, _, quoted, topical = tier_for_claim("hired a new VP of Engineering", page)
    assert tier in (TIER_LOW_MATCH, TIER_UNSUPPORTED)
    assert quoted < 0.55


def test_tier_for_claim_unverified_on_thin_page():
    tier, note, quoted, topical = tier_for_claim("anything", "too short")
    assert tier == TIER_UNVERIFIED
    assert quoted == 0.0 and topical == 0.0


def test_disqualifying_tiers_exclude_verified_and_low_match():
    assert TIER_BROKEN in TIER_DISQUALIFYING
    assert TIER_UNSUPPORTED in TIER_DISQUALIFYING
    assert TIER_VERIFIED not in TIER_DISQUALIFYING
    assert TIER_LOW_MATCH not in TIER_DISQUALIFYING


def test_claim_signals_empty_inputs_return_zero():
    assert claim_signals("", LONG_PAGE) == (0.0, 0.0)
    assert claim_signals("something", "") == (0.0, 0.0)
