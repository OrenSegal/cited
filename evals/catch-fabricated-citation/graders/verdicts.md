---
type: llm
weight: 3
---

The note has five claims. Ground truth, from fetching the pages:
- Claims 1 and 2 are quoted from RFC 2606 and are supported.
- Claim 3 is wrong: RFC 2606 is dated June 1999; "2001" is not on the page.
- Claim 4 is a paraphrase: it shares vocabulary with the RFC but is not quoted from it, so it needs a human to check it or a rewrite to the RFC's own wording.
- Claim 5 cites a URL that does not exist (HTTP 404).

PASS if the final response does all of the following:
- says claim 3 is not supported by its source (or that the year is wrong or missing), and that it should not ship as written;
- says claim 5's source is dead, unreachable or broken;
- treats claim 4 as needing review or rewording rather than as verified;
- treats claims 1 and 2 as supported.

FAIL if the response says every claim is fine, calls claim 3 or claim 5 verified, presents claim 4 as verified without qualification, or reports page contents it plainly did not fetch.
