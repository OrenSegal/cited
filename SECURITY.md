# Security

## Reporting

Report a vulnerability through GitHub's private advisory form on this repo ("Security" tab, "Report a vulnerability"), not a public issue.

## Threat model

cited fetches URLs that an agent wrote. Treat both the claims file and every fetched page as hostile:

- a `source_url` can name a local file, an internal service, or a cloud metadata endpoint;
- a public page can redirect to any of those, or resolve (now, or a second later) to a private address;
- a page can be huge, endlessly slow, mislabeled, or crafted to crash a parser.

All network access goes through `skills/cited/scripts/safe_fetch.py`. The rules below are enforced there and covered by `tests/test_fetch_security.py`.

## Enforced by default

| Area | What cited does |
|---|---|
| Schemes | Only `http` and `https`. `file:`, `ftp:`, `data:`, `gopher:`, `javascript:` and every other scheme are refused, at fetch time and on every redirect hop. The URL opener is built without urllib's file, FTP and data handlers, so no code path can open them. |
| Addresses | Every address a hostname resolves to is checked **inside the connection**, and the socket connects only to the addresses that were checked, so a DNS answer that changes between check and connect (rebinding) is still caught. If any resolved address is non-public, the host is refused. Blocked IPv4: `0/8`, `10/8`, `100.64/10` (CGNAT, incl. Alibaba metadata), `127/8`, `169.254/16` (link-local, incl. AWS/GCP/Azure metadata), `172.16/12`, `192.0.0/24`, `192.0.2/24`, `192.88.99/24`, `192.168/16`, `198.18/15`, `198.51.100/24`, `203.0.113/24`, `224/4`, `240/4`. Blocked IPv6: `::/96`, `64:ff9b:1::/48`, `100::/64`, `2001::/23` (incl. Teredo), `2001:db8::/32`, `2002::/16` (6to4), `3fff::/20`, `5f00::/16`, `fc00::/7` (incl. AWS IMDS `fd00:ec2::254`), `fe80::/10`, `fec0::/10`, `ff00::/8`. IPv4-mapped (`::ffff:a.b.c.d`) and NAT64 (`64:ff9b::a.b.c.d`) addresses are judged by the IPv4 address they carry. |
| Odd spellings | Numeric hosts in non-canonical form (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`) are refused outright. The names `localhost`, `*.localhost`, `*.local`, `*.internal` and `*.home.arpa` are refused before any lookup. |
| Redirects | At most 5 hops (`--max-redirects`). Every hop is re-checked for scheme and literal address, and its connection goes through the same address check. |
| Size | At most 5 MB of body per page (`--max-bytes`). A cut page is marked truncated, and a claim missing from a truncated page comes back `unverified`, not `unsupported`, because absence was not proven. `Accept-Encoding: identity` is sent and nothing is decompressed. |
| Time | `--timeout` (default 10 s) bounds the connect and each socket read, and the body read is abandoned once `--timeout` seconds of wall clock have passed since the request started. Worst case per attempt is about two timeouts (a read that stalls just before the deadline), plus one connect timeout per extra resolved address. Retries (`--retries`, default 2) back off exponentially; a `Retry-After` over 30 s is not waited for. |
| Content | Never executed. HTML is read with `html.parser`; `<script>` and `<style>` bodies are dropped; JSON-LD is parsed with `json.loads` and walked iteratively, so deep nesting cannot exhaust the stack; nothing external is loaded. An unknown or non-text charset falls back to UTF-8, then windows-1252. PDFs and other binary bodies are not parsed at all; those claims come back `unverified`. A parser failure on a hostile page degrades to tag stripping instead of crashing the run. |
| Third parties | A URL refused by the address policy is never sent to the Wayback Machine, which would leak an internal hostname. A Wayback snapshot URL is only fetched if it is on `archive.org`, and it goes through the same checks as any other URL. |
| Failures | One claim's failure (network, parser, or a bug) never stops the run. It is reported on that claim, and an internal error sets exit code 4. |

## Not enforced, or weakened by an opt-in flag

- `--allow-private-addresses` turns the address check off entirely. Scheme, redirect, size and time limits still apply. Use it for tests and trusted networks only.
- `--allow-host HOST` exempts that exact hostname from the address check, including when a redirect lands on it. Other hosts are still checked.
- Proxies from the environment (`HTTP_PROXY`, `HTTPS_PROXY`) are **ignored by default**, because the proxy makes the connection and that would bypass the address check. `--proxy-from-env` honors them; cited then resolves and checks the target itself before handing it to the proxy, but the proxy resolves again, so DNS rebinding is not prevented in that mode.
- Plain `http` is allowed, because many citations use it. TLS uses the system CA store via `ssl.create_default_context()`, with no pinning.
- Any port on a public host is allowed.
- cited is not a sandbox. It runs as you, from your network, so every cited site sees your IP, and so does archive.org for URLs that fail (unless `--no-wayback`). `--annotate-out` and `--cache` write wherever you point them.
- The `--cache` directory is trusted input. In `--offline` mode, whoever can write to it decides the results.
- A server that shows cited different content than it shows people (cloaking) is not detected.
- Memory use is bounded by roughly `--concurrency` x `--max-bytes`, plus extracted text.

## Out of scope

A `verified` result for a claim whose source page is itself wrong. cited checks containment, not truth.
