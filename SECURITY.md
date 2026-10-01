# Security

cited fetches URLs that an agent supplies. Treat the claims file as untrusted input.

- Report a vulnerability through GitHub's private advisory form on this repo, not a public issue.
- In scope: the fetcher following a redirect or URL scheme it should not (for example `file:` or an internal address), and any path that executes content from a fetched page.
- Out of scope: a `verified` result for a claim whose source page is itself wrong. cited checks containment, not truth.
