# Contributing

## Setup

```bash
git clone https://github.com/OrenSegal/cited.git
cd cited
python3 -m pip install -r requirements-dev.txt   # pytest and ruff, for development only
```

cited itself is Python 3.10+ and standard library only. Keep it that way: no runtime dependencies.

## Checks

The same commands CI runs:

```bash
ruff check .
shellcheck bin/cited
python3 -m pytest tests -v
```

And, with Claude Code installed:

```bash
claude plugin validate .claude-plugin/plugin.json --strict
claude plugin validate .claude-plugin/marketplace.json --strict
claude plugin validate commands --strict
claude plugin validate skills --strict
```

The test suite makes no real network calls.

## Layout

| Path | What it is |
|---|---|
| `skills/cited/scripts/tiering_core.py` | Pure scoring: containment, the specifics check, the tier for a claim against page text, which tiers block. No I/O. |
| `skills/cited/scripts/safe_fetch.py` | The only code that opens a socket. Scheme, address, redirect, size and time limits (`FetchPolicy`). `fetch_page` never raises. |
| `skills/cited/scripts/page_text.py` | Content classification, charset decoding and HTML-to-text. Parses, never executes. |
| `skills/cited/scripts/fetcher.py` | `Fetcher`: retries, per-host throttle, one fetch per URL per run, the disk cache, the Wayback lookup, bot-walled sites. |
| `skills/cited/scripts/verify_claims.py` | The CLI. `load_claims` (input validation), `check_source` (the decision tree that turns fetch results into a tier), report and exit codes. |
| `bin/cited` | POSIX sh wrapper that finds Python 3.10+ and runs `verify_claims.py`. On PATH when the plugin is enabled. |
| `commands/check.md`, `skills/cited/SKILL.md` | The `/cited:check` command and the skill. |
| `evals/` | `claude plugin eval` cases. Not run in CI. |

## Tests

| File | Covers |
|---|---|
| `test_tiering_core.py` | Scoring with fixed strings. |
| `test_fetch_security.py` | The address policy: schemes, private and metadata addresses, odd IP spellings, DNS answers, redirects, size and time caps. |
| `test_verify_claims.py` | `check_source` branching (live, thin, Wayback, bot walls) through a `StubFetcher` that returns canned `FetchResult`s. |
| `test_cli.py` | Input validation, exit codes, the JSON schema, cache and `--offline`, retries, throttling, decoding. |
| `test_docs.py` | The README, SECURITY.md and SKILL.md match the code: tiers, exit codes, flag defaults, the JSON shape, blocked networks. |
| `test_integration.py` | End to end over real HTTP against a local server (`conftest.FixtureServer`) serving `tests/fixtures/site/`, through `main()` and through `bin/cited`. Every tier, and Wayback via a local stand-in. |

`conftest.py` provides the `server` fixture (a threaded HTTP server on 127.0.0.1 with per-path hit counts) and `fake_dns`, which makes a hostname resolve to an address of your choice for one test. Because the fixture server is on loopback, tests that fetch from it pass `--allow-private-addresses` (or `FetchPolicy(allow_private=True)`); `test_default_policy_refuses_the_local_fixture_server` checks the default still refuses it.

## Making changes

- **Fixes come with a test that fails without them.** For a fetcher bug, add it to `test_fetch_security.py` or `test_cli.py`; for a wrong tier, add a page to `tests/fixtures/site/` and a claim to `tests/fixtures/claims.json` with its `expect` tier.
- **Network code goes through `safe_fetch.fetch_page`.** Do not make requests with `urllib.request`, `http.client` or raw sockets anywhere else; the address checks only cover that path. A change that loosens the policy must update SECURITY.md in the same pull request.
- **`check_source` is the decision tree.** If you change its branching, cover the new branch in `test_verify_claims.py` with a `StubFetcher`.
- **Scoring stays pure.** `tiering_core.py` takes strings and returns numbers and tiers, so it is trivial to test.
- **The output is an interface.** Exit codes and the JSON report are documented in the README, and `test_docs.py` fails if the two drift. Adding a field is fine; renaming or removing one, or changing an exit code, needs a `schema_version` bump and a CHANGELOG entry.
- **Containment, not truth.** cited reports whether a claim is on the cited page. Do not add logic that infers a claim is true. See `skills/cited/references/methodology.md`.

## Evals

```bash
claude plugin eval . --allow-tools Bash Write "WebFetch(domain:www.rfc-editor.org)" "WebFetch(domain:archive.org)"
```

They call a model and cost money. Run them when you change `SKILL.md`, `commands/check.md`, or anything that changes what the skill sees. Results land in `evals/results/`, which is ignored by git.

## Releases

Bump the version in `skills/cited/scripts/safe_fetch.py` (`VERSION`), `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` together (a test checks they match), and add a CHANGELOG entry.

## Pull requests

Open against `main` and fill in the template. Describe the problem, not just the change. Be decent; see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
