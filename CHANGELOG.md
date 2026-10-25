# Changelog

## Unreleased

### Added

- Markdown drafts as input: `cited draft.md` finds every inline link, works out which claim each one supports, and checks those pairs. Links with no claim attached (a bare "here", a bibliography entry, a relative path) are listed, never fetched, and never fail the run. `--extract-only` prints the pairs without touching the network. In `--json`, results gain `line` and `bucket`, and the report gains a `draft` object.
- `--certificate PATH` writes an HTML certificate of the run, for a draft or a claims file.
- The extractor (`extract.py`), its regression tests and the certificate (`certificate.py`) come from receipts by Oren Segal (MIT), a standalone tool that turned prose with links into claim/source pairs and wrote a certificate. receipts' own fetcher, tiering and specifics audit were not ported: cited's `safe_fetch`, `check_source` and specifics check already cover them.
- `hosted/`: a stdlib web front end, deployable as a Vercel Python function (`api/index.py`, `vercel.json`). A free link check (dead and redirected sources, first 25 links, nothing stored) and a certificate gated by single-use access codes, stored locally or in Supabase Storage and served at `/c/{id}`. See `hosted/README.md`.
- Bare URL citations: the extractor also reads a URL after a claim (including in a bracketed tag such as `[S: https://...]`), a table row whose cell is a URL (the claim is the row's first prose cell), and a scheme-less `(domain.tld/path)` reference, which gets `https://` and says so in its reason. Found on real research notes, which cited with these and extracted to zero pairs. Each bare URL's claim is the clause nearest it (split at ';', sentence ends and table cells), not the whole sentence, so a sentence citing three sources is no longer checked whole against each.
- Hosted certificates run as polled jobs (each visit to `/c/{id}` checks batches within a time budget), with a per-address rate limit on `POST /check`, web page URLs as drafts, a private delete link, `hosted/recheck.py` for the 30-day re-check, and an unverified Stripe webhook that mints access codes.
- `verify_claims.check_entries` runs the check for a list of entries and returns the report rows; `main()` uses it. `certificate.render` takes `footer_link`, which signs the footer "Checked by cited" with a link. `safe_fetch.fetch_page` takes `keep_body`, which returns the raw body in `extra`.

### Changed

- The certificate leads with the verdict, a tally of tiers, and claims grouped by tier, and follows the reader's light or dark setting. A Wayback verdict note joins its parts with a semicolon instead of a dash.

### Fixed

- `--proxy-from-env` fetches no longer come back short behind Claude Code's sandbox proxy. urllib always sent `Connection: close`, and the proxy dropped the tail of the body when the server closed; proxy mode now asks to keep the connection open. The direct path is unchanged.
- The evals measure the skill text: `invalid-claims-file` needs a real `cited` run on a claims file and fails with the plugin absent, `catch-fabricated-citation` runs offline against recorded pages and checks the disclosed tally, and `cited:check` passes `--proxy-from-env` in a sandbox.

## 0.3.1

### Changed

- `cited --help` lists the exit codes and the `Retry-After` cap.
- Page decoding and text extraction moved to `page_text.py`, and retries, caching and the Wayback lookup to `fetcher.py`. Behavior is unchanged.
- The README tier table, exit codes, option defaults and JSON example, and the blocked networks in SECURITY.md, are checked against the code by `tests/test_docs.py`.
- `/cited:check` runs `${CLAUDE_PLUGIN_ROOT}/bin/cited` when `cited` is not on PATH, instead of searching the filesystem for the script.
- The `catch-fabricated-citation` eval graders match the namespaced skill name and a real `cited` run by name or path, and no longer count `cited --help` or `which cited` as a run.

## 0.3.0

### Security

- The fetcher no longer reaches internal addresses. Only `http` and `https` are fetched; `file:`, `ftp:` and `data:` URLs (which were read before) are refused. Loopback, private, link-local, cloud-metadata, CGNAT, multicast and reserved addresses are refused for IPv4 and IPv6, including IPv4-mapped and NAT64 forms, decimal, hex and short IP spellings, hostnames that resolve to such an address, and redirects to any of them. The check runs on the address actually connected to, so DNS rebinding does not get around it. `--allow-host`, `--allow-private-addresses` and `--proxy-from-env` relax it explicitly. SECURITY.md lists what is and is not enforced.
- Redirects (5), response size (5 MB) and total time per request are capped. Compressed responses are not requested.
- The Wayback snapshot URL is checked like any other URL.

### Fixed

- A page cut off at the size cap was reported `unsupported` when the claim was in the unread part. It is now `unverified`.
- A page in an unknown or wrong charset was reported `broken`. Bodies are decoded from the header, then the page's own `<meta charset>`, then UTF-8 or cp1252.
- Deeply nested HTML or JSON-LD crashed the run with `RecursionError`.
- Wayback captures were fetched with the archive's toolbar wrapper, so an archived PDF was read as a short HTML page and the claim reported `unsupported`. Captures are now fetched raw (`id_`), archived PDFs come back `unverified`, and captures of error pages are not used.
- An invalid claims file produced a traceback.

### Added

- `unverified` tier for sources that were never checked: PDFs and other binary files, thin pages, truncated pages, and `--offline` cache misses. It blocks only under `--strict`.
- Input validation that lists every problem in the claims file and exits 2 before fetching anything. `-` reads from stdin.
- Documented exit codes: 0 ok, 1 blocking, 2 usage or input error, 3 offline cache miss, 4 internal error, 130 interrupted.
- `--json`: a versioned report (`schema_version` 1), documented in the README.
- `--cache DIR` and `--offline` for reproducible runs and CI.
- Concurrency (`--concurrency`), a per-host delay (`--per-host-delay`), retries with backoff that honor `Retry-After` (`--retries`), and one fetch per distinct URL.
- `--timeout`, `--max-bytes`, `--max-redirects`, `--no-wayback`, `--version`.
- `bin/cited`, on PATH when the plugin is enabled, and the `/cited:check` command.
- A `claude plugin eval` suite under `evals/` (not run in CI).
- Offline end-to-end tests that cover every tier over real HTTP against a local fixture server.
- CI on Python 3.10 to 3.13 with ruff and shellcheck, and `claude plugin validate --strict` on the manifest, marketplace, commands and skills. Dependabot, issue and PR templates, code of conduct.

## 0.2.0

- Renamed from verify-before-ship to cited. The old GitHub URL redirects.
- Now a valid Claude Code plugin: `.claude-plugin/plugin.json`, with the skill at `skills/cited/`.
- The script and references moved with the skill to `skills/cited/scripts/` and `skills/cited/references/`.
- CI runs `claude plugin validate`.

## 0.1.0

- Containment check with tiers `verified`, `low_match`, `snippet_only`, `unsupported`, `broken`.
