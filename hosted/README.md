# cited, hosted

A small web front end for cited: paste a Markdown draft, or link to a raw Markdown file, and get one of two things.

| Mode | Price | What it does | Stored? |
|---|---|---|---|
| Free link check | Free | Fetches the first 25 distinct http(s) links and says which are dead, redirected, blocking automated readers, or fine. No claim checking. | No |
| Certificate | $9, via a Stripe Payment Link and an access code | Runs the full cited check on every claim (Verified, Not on page, Broken source, and the other tiers), renders the same HTML certificate as `cited draft.md --certificate`, and stores it at `/c/{id}`. | Yes |

It is standard library Python, like the rest of cited, and reuses cited's own modules: `extract` for the draft, `verify_claims.check_entries` for the run, `certificate.render` for the page, and `safe_fetch` for every request.

## Run it locally

```sh
CITED_ACCESS_CODES=try-me python3 hosted/webapp.py
```

Open http://127.0.0.1:8000. Use `try-me` as the access code for a certificate (each code works once). Certificates are written to `hosted/.data/` (gitignored). Set `PORT` to use another port.

Tests run with the rest of the suite: `python -m pytest tests -v` (the hosted tests are `tests/test_hosted.py`).

## Environment variables

| Variable | Needed | Meaning |
|---|---|---|
| `CITED_ACCESS_CODES` | For certificates | Comma-separated access codes. Each works once: the first certificate made with a code spends it. With none set, certificates are switched off and the free check still works. |
| `CITED_PAYMENT_LINK` | For launch | The Stripe Payment Link for a $9 certificate. Until it is set, the page links to the placeholder `STRIPE_PAYMENT_LINK_TODO`. |
| `SUPABASE_URL` | On Vercel | Your project URL, for example `https://abcd.supabase.co`. |
| `SUPABASE_SERVICE_KEY` | On Vercel | The project's service role key. Server side only, never shown to a browser. |
| `SUPABASE_BUCKET` | On Vercel | A **private** Storage bucket for certificates and spent codes, for example `certificates`. |
| `CITED_DATA_DIR` | No | Local storage directory when Supabase is not configured. Default `hosted/.data`. |

Set all three Supabase variables or none. On Vercel (`VERCEL` is set) the app refuses to fall back to local disk, because a function's disk does not outlive the request: certificates would vanish. The free check works without storage.

## How it is built, and why

- **Vercel Python function, stdlib `BaseHTTPRequestHandler`.** `api/index.py` (at the repo root) exposes `webapp.Handler` as `handler`, and `vercel.json` rewrites every path to it. The same handler class runs the local server, so local and deployed behaviour are the same code. No framework, no `requirements.txt`, nothing to install.
- **Why the entry point is at the repo root.** The app imports cited's scripts from `skills/cited/scripts/`. With the Vercel project rooted at the repo, those files are in the function bundle with no copy step. `excludeFiles` keeps tests, evals and the plugin files out of it.
- **Synchronous runs.** A certificate is checked inside the request, then the browser is redirected to `/c/{id}`. There is no job queue yet. `maxDuration` is 60 seconds; the fetch timeout is 8 seconds per page, 8 pages at a time, one retry, Wayback fallback on. A draft with many slow sources can run past 60 seconds; see Gaps.
- **Every fetch is guarded.** Draft URLs and source URLs both go through `safe_fetch`: http(s) only, every resolved address must be public (loopback, private, link-local and cloud metadata addresses, CGNAT and the rest are refused, checked at connect time so DNS rebinding cannot slip past), redirects re-checked on every hop and capped at 5, bodies capped (500 KB for a draft, 5 MB for a source), and wall-clock timeouts. The hosted app never sets `allow_private`; only the tests do.
- **Certificate pages** are the CLI certificate with one addition: the footer reads "Checked by cited" and links to the repo. They are served with a strict Content-Security-Policy (no scripts, no external loads), `nosniff`, and `Referrer-Policy: no-referrer` so a reader clicking a source does not leak the certificate URL. IDs are 16 random URL-safe characters and are validated before any storage lookup.
- **Entitlement is manual.** A buyer pays through the Payment Link; you email them a code from `CITED_ACCESS_CODES`. Codes are single use: the app atomically creates `codes/<sha256 of code>` in storage and refuses a code whose marker exists. A code is spent only after the draft is known to have checkable claims.

## Deploying (the owner does this; nothing here deploys itself)

1. **Supabase Storage.** In a Supabase project (a new one, or an existing non-production one), create a **private** bucket, for example `certificates`. Copy the project URL and the service role key (Project Settings, API).
2. **Stripe.** Create a product "cited certificate" at $9 and a Payment Link for it. In the link's confirmation settings, show a message such as "We will email your access code within one business day." Copy the link URL.
3. **Access codes.** Generate a few, for example `python3 -c "import secrets; print(','.join(secrets.token_urlsafe(9) for _ in range(20)))"`. Keep a list of which you have handed out.
4. **Vercel project.** Import the `cited` GitHub repo in Vercel. Root Directory: the repo root (leave it blank). Framework preset: Other. No build command.
5. **Environment variables** (Project Settings, Environment Variables, Production and Preview): `CITED_ACCESS_CODES`, `CITED_PAYMENT_LINK`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET`.
6. **Deploy a preview first** and check, in this order:
   - `GET /healthz` returns `ok`.
   - `GET /` shows the form, and "Buy a certificate" points at your Stripe link.
   - A free link check on a short draft returns a table.
   - A free link check on a draft linking to `http://169.254.169.254/` shows **Refused**.
   - A certificate with a test code redirects to `/c/{id}`, and that page reloads after a fresh deploy (proves Supabase storage, not local disk).
   - Reusing the same code is refused.
   - The function log shows no request path problems with the rewrite (`/c/{id}` must reach the function as `/c/{id}`).
7. Promote to production and add a domain if you want one.
8. **Fulfilment, per sale:** Stripe emails you the payment; reply to the buyer with one unused code. Remove spent codes from `CITED_ACCESS_CODES` from time to time (the app already refuses them).

## Gaps and shortcuts in this slice

- **No job queue.** Runs are synchronous inside one function call. Long drafts (more than about 40 slow sources) can hit the 60-second `maxDuration`. Raise it if your plan allows, or move runs to a queue plus worker and poll for the result.
- **No rate limiting.** Anyone can run the free check. Each request is capped (25 links, 1 MB form, guarded fetches), but there is no per-IP limit. Turn on Vercel's firewall rate limiting for `POST /check` before sharing the URL widely.
- **Markdown only.** A URL must point at a Markdown or plain-text file. HTML pages are refused with a message; extracting links from rendered HTML is not built.
- **Manual entitlement.** No Stripe webhook, no accounts, no email. A failed run after a code is spent (a timeout, a storage error) burns the code; reissue one by hand.
- **No re-checks.** The $9 tier promises a 30-day re-check in the pricing plan; nothing schedules one yet. Run JSON is stored next to each certificate (`certs/{id}.json`) so a later re-check can diff against it.
- **Certificates are public by link.** Anyone with the URL can read one, and there is no way to delete one from the app.
- **Untested against live Supabase and Vercel.** The Supabase adapter is tested against a fake HTTP opener, and the Vercel entry point and rewrite are untested until the first preview deploy (step 6).
