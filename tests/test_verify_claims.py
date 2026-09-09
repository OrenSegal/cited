import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from tiering_core import TIER_BROKEN, TIER_SNIPPET_ONLY
from verify_claims import (
    _TextExtractor,
    bot_walled_host,
    canonicalize_for_fetch,
    check_source,
    is_challenge_page,
)


def test_bot_walled_host_matches_exact_and_subdomain():
    assert bot_walled_host("https://reddit.com/r/foo") == "reddit.com"
    assert bot_walled_host("https://www.reddit.com/r/foo") == "reddit.com"
    assert bot_walled_host("https://old.reddit.com/r/foo") == "reddit.com"


def test_bot_walled_host_none_for_unlisted_domain():
    assert bot_walled_host("https://example.com/page") is None


def test_canonicalize_rewrites_reddit_to_old_reddit():
    assert canonicalize_for_fetch("https://www.reddit.com/r/foo") == "https://old.reddit.com/r/foo"
    assert canonicalize_for_fetch("https://reddit.com/r/foo") == "https://old.reddit.com/r/foo"


def test_canonicalize_leaves_other_urls_untouched():
    url = "https://example.com/careers"
    assert canonicalize_for_fetch(url) == url


def test_is_challenge_page_detects_short_verification_stub():
    assert is_challenge_page(200, "Please wait for verification, we are checking your browser.")


def test_is_challenge_page_false_for_normal_content():
    assert not is_challenge_page(200, "A normal page with real content. " * 20)


def test_is_challenge_page_false_for_non_200():
    assert not is_challenge_page(403, "please wait for verification")


def test_text_extractor_pulls_visible_text():
    parser = _TextExtractor()
    parser.feed("<html><body><p>Hello world</p></body></html>")
    assert "Hello world" in parser.text()


def test_text_extractor_skips_script_and_style():
    parser = _TextExtractor()
    parser.feed("<html><body><script>var x=1;</script><style>.a{}</style><p>Real text</p></body></html>")
    text = parser.text()
    assert "Real text" in text
    assert "var x" not in text


def test_text_extractor_captures_meta_description():
    parser = _TextExtractor()
    parser.feed('<html><head><meta name="description" content="A meta claim here"></head><body></body></html>')
    assert "A meta claim here" in parser.text()


def test_text_extractor_captures_ldjson_string_values():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"name": "Acme raised a Series A"}'
        "</script></head><body></body></html>"
    )
    parser = _TextExtractor()
    parser.feed(html)
    assert "Acme raised a Series A" in parser.text()


def test_check_source_falls_back_to_wayback_when_live_fetch_fails():
    with patch("verify_claims.fetch_text", return_value=(None, "")), patch(
        "verify_claims.fetch_wayback",
        return_value=("https://web.archive.org/x", "20260101", "The claim text is right here."),
    ):
        tier, note, quoted, topical = check_source("https://example.com", "claim text", 10)
    assert "Wayback archive" in note
    assert tier != TIER_BROKEN


def test_check_source_broken_when_live_and_wayback_both_fail():
    with patch("verify_claims.fetch_text", return_value=(None, "")), patch(
        "verify_claims.fetch_wayback", return_value=("", "", "")
    ):
        tier, note, quoted, topical = check_source("https://example.com", "claim text", 10)
    assert tier == TIER_BROKEN


def test_check_source_snippet_only_on_rate_limit_with_no_archive():
    with patch("verify_claims.fetch_text", return_value=(429, "")), patch(
        "verify_claims.fetch_wayback", return_value=("", "", "")
    ):
        tier, note, quoted, topical = check_source("https://example.com", "claim text", 10)
    assert tier == TIER_SNIPPET_ONLY
    assert "rate-limited" in note.lower()


def test_check_source_snippet_only_for_bot_walled_domain_403():
    with patch("verify_claims.fetch_text", return_value=(403, "")), patch(
        "verify_claims.fetch_wayback", return_value=("", "", "")
    ):
        tier, note, quoted, topical = check_source("https://reddit.com/r/x", "claim text", 10)
    assert tier == TIER_SNIPPET_ONLY
    assert "reddit.com" in note
