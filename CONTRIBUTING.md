# Contributing

## Setup

```bash
git clone https://github.com/OrenSegal/cited.git
cd cited
```

Python 3.10+, stdlib only — no dependencies to install for the tool itself. `pytest` is needed to run the test suite.

## Running tests

```bash
python3 -m pytest tests/ -v
```

Same command CI runs. `test_tiering_core.py` covers the pure scoring logic (`tiering_core.py`) with fixed strings — no network. `test_verify_claims.py` covers `verify_claims.py`'s URL/HTML handling (bot-wall detection, Reddit canonicalization, challenge-page detection, the HTML text extractor including meta/JSON-LD) and `check_source`'s fallback branching (live-fetch failure → Wayback, rate-limit, bot-walled 403) by mocking `fetch_text`/`fetch_wayback` — no real HTTP calls in the suite.

## Making changes

- `check_source` is the core decision tree (live fetch → too-thin/failed → Wayback fallback → rate-limit/bot-wall → tier). If you touch its branching, add a test that would fail without your change — mock `fetch_text`/`fetch_wayback` rather than hitting real URLs.
- Containment scoring lives in `tiering_core.py`, deliberately decoupled from HTTP/HTML concerns so it stays trivial to test with fixed strings. Keep it that way.
- This tool proves containment, not truth — don't add logic that infers a claim is "true," only whether it's actually on the cited page. See `skills/cited/references/methodology.md`.

## Pull requests

Open against `main`. Describe the problem, not just the change.
