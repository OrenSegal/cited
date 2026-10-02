# Changelog

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
