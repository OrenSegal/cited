from fetcher import bot_walled_host, canonicalize_for_fetch, is_challenge_page
from page_text import _TextExtractor
from safe_fetch import FetchPolicy, FetchResult
from tiering_core import TIER_BROKEN, TIER_SNIPPET_ONLY, TIER_UNVERIFIED
from verify_claims import check_source


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


class StubFetcher:
    """Stands in for fetcher.Fetcher: canned live and archived results."""

    def __init__(self, live, archived=None, use_wayback=True):
        self.live = live
        self.archived = archived
        self.use_wayback = use_wayback
        self.policy = FetchPolicy()
        self.wayback_calls = 0

    def page(self, url):
        return self.live

    def wayback(self, url):
        self.wayback_calls += 1
        if self.archived is None:
            return "", "", None
        return "https://web.archive.org/x", "20260101", self.archived


ARCHIVED = FetchResult(status=200, kind="html", text="The claim text is right here. " * 10)


def test_check_source_falls_back_to_wayback_when_live_fetch_fails():
    verdict = check_source("https://example.com", "claim text", StubFetcher(FetchResult(error="timed out"), ARCHIVED))
    assert "Wayback archive" in verdict.note
    assert verdict.tier != TIER_BROKEN
    assert verdict.checked_against == "wayback"


def test_check_source_broken_when_live_and_wayback_both_fail():
    verdict = check_source("https://example.com", "claim text", StubFetcher(FetchResult(error="timed out")))
    assert verdict.tier == TIER_BROKEN


def test_check_source_snippet_only_on_rate_limit_with_no_archive():
    verdict = check_source("https://example.com", "claim text", StubFetcher(FetchResult(status=429)))
    assert verdict.tier == TIER_SNIPPET_ONLY
    assert "rate-limited" in verdict.note.lower()


def test_check_source_snippet_only_for_bot_walled_domain_403():
    verdict = check_source("https://reddit.com/r/x", "claim text", StubFetcher(FetchResult(status=403)))
    assert verdict.tier == TIER_SNIPPET_ONLY
    assert "reddit.com" in verdict.note


def test_check_source_blocked_url_is_broken_and_never_sent_to_wayback():
    stub = StubFetcher(FetchResult(error="refused: address 10.0.0.1 is not a public address", blocked=True), ARCHIVED)
    verdict = check_source("http://10.0.0.1/", "claim text", stub)
    assert verdict.tier == TIER_BROKEN
    assert stub.wayback_calls == 0


def test_check_source_pdf_is_unverified_not_unsupported():
    verdict = check_source("https://example.com/a.pdf", "claim text",
                           StubFetcher(FetchResult(status=200, kind="pdf", content_type="application/pdf")))
    assert verdict.tier == TIER_UNVERIFIED
    assert "PDF" in verdict.note


def test_check_source_truncated_page_cannot_prove_absence():
    page = FetchResult(status=200, kind="html", truncated=True, text="Unrelated filler content about gardening. " * 20)
    verdict = check_source("https://example.com", "Acme raised a $12M Series A led by Sequoia", StubFetcher(page))
    assert verdict.tier == TIER_UNVERIFIED
    assert "not proven" in verdict.note


def test_bot_walled_host_ignores_port_and_userinfo():
    assert bot_walled_host("https://user@www.reddit.com:443/r/foo") == "reddit.com"


def test_canonicalize_keeps_port():
    assert canonicalize_for_fetch("https://reddit.com:8443/r/foo") == "https://old.reddit.com:8443/r/foo"
