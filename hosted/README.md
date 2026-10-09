# cited, hosted

A small web front end for cited: paste a Markdown draft, or link to a raw Markdown file or a web page, and get one of two things.

| Mode | Price | What it does | Stored? |
|---|---|---|---|
| Free link check | Free | Fetches the first 25 distinct http(s) links and says which are dead, redirected, blocking automated readers, or fine. No claim checking. | No |
| Certificate | $9, via a Stripe Payment Link and an access code | Runs the full cited check on every claim (Verified, Not on page, Broken source, and the other tiers), renders the same HTML certificate as `cited draft.md --certificate`, and stores it at `/c/{id}`. The owner gets a private delete link. | Yes, until deleted |

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
| `CITED_RATE_LIMIT` | No | `POST /check` limit per client address, as `count/seconds`. Default `10/600`. `0/600` turns it off. |
| `STRIPE_WEBHOOK_SECRET` | Only for the webhook | The `whsec_...` signing secret of a Stripe webhook endpoint pointed at `/stripe/webhook`. Unset: the endpoint answers 503 and codes come only from `CITED_ACCESS_CODES`. |

Set all three Supabase variables or none. On Vercel (`VERCEL` is set) the app refuses to fall back to local disk, because a function's disk does not outlive the request: certificates would vanish. The free check works without storage.

## How it is built, and why

- **Vercel Python function, stdlib `BaseHTTPRequestHandler`.** `api/index.py` (at the repo root) exposes `webapp.Handler` as `handler`, and `vercel.json` rewrites every path to it. The same handler class runs the local server, so local and deployed behaviour are the same code. No framework, no `requirements.txt`, nothing to install.
- **Why the entry point is at the repo root.** The app imports cited's scripts from `skills/cited/scripts/`. With the Vercel project rooted at the repo, those files are in the function bundle with no copy step. `excludeFiles` keeps tests, evals and the plugin files out of it.
- **Certificates are jobs that the reader's browser drives.** Vercel's Python runtime stops a function when its response is sent, so a background thread cannot outlive the request, and one request has 60 seconds. So `POST /check` only validates the draft, spends the code and stores the claims (`jobs/{id}.json`); it answers at once with the share link and the delete link. Each visit to `/c/{id}` while the job is pending checks whole batches of 8 claims (one round of parallel fetches: 6 s timeout, one retry, Wayback fallback) and stops starting batches once the next one could push the visit past a 50-second budget. A batch's worst case is about 41 seconds, so a visit cannot reach 60. The page refreshes itself every second (`<meta http-equiv="refresh">`, which needs no script, so the strict CSP stays) and shows "n of m checked"; when every batch is stored, the visit renders the certificate. Each batch's rows are stored under their own key (`jobs/{id}/b{n}.json`), so two tabs never overwrite each other, and a visitor claims a batch with an atomic `create` of a lease key, so two tabs do not fetch the same batch. If everyone closes the tab, the job waits and resumes on the next visit. Chosen over a queue and worker because it needs nothing beyond the one function and the storage it already has; the cost is that a certificate only progresses while someone has it open. In the real run (RUNLOG.md) 113 claims took 4 visits, the longest 21 s.
- **Rate limit inside the app.** `POST /check` allows 10 requests per 10 minutes per client address. Each request claims a numbered slot key (`ratelimit/{window}/{hash of address}/{n}`) with `create`, so the count is shared by every function instance. Without storage it counts in process memory, which on serverless is per warm instance only. Addresses are stored hashed. On Vercel the address comes from `X-Real-IP`/`X-Forwarded-For`, which Vercel sets; elsewhere those headers are ignored. Limits: slot keys are never cleaned up (add a bucket lifecycle rule, or prune `ratelimit/` by hand), a storage outage fails open, and a fixed window allows a burst of up to 20 across a window boundary.
- **Web pages as drafts.** A URL that serves HTML is converted with `html.parser` (`hosted/htmldraft.py`): headings, paragraphs, list items, table rows and `<a href>` as Markdown links resolved against the page URL. Navigation chrome (nav, header, footer, aside, forms) and hidden elements are dropped. The page and every link in it are fetched through `safe_fetch` like any other source.
- **Every fetch is guarded.** Draft URLs and source URLs both go through `safe_fetch`: http(s) only, every resolved address must be public (loopback, private, link-local and cloud metadata addresses, CGNAT and the rest are refused, checked at connect time so DNS rebinding cannot slip past), redirects re-checked on every hop and capped at 5, bodies capped (500 KB for a draft, 5 MB for a source), and wall-clock timeouts. The hosted app never sets `allow_private`; only the tests do.
- **Certificate pages** are the CLI certificate with one addition: the footer reads "Checked by cited" and links to the repo. They are served with a strict Content-Security-Policy (no scripts, no external loads), `nosniff`, and `Referrer-Policy: no-referrer` so a reader clicking a source does not leak the certificate URL. IDs are 16 random URL-safe characters and are validated before any storage lookup.
- **Entitlement.** A buyer pays through the Payment Link; you email them a code from `CITED_ACCESS_CODES`. Codes are single use: the app atomically creates `codes/<sha256 of code>` in storage and refuses a code whose marker exists. A code is spent only after the draft is known to have checkable claims. Optionally, `POST /stripe/webhook` (checkout.session.completed, signature checked against `STRIPE_WEBHOOK_SECRET` with a 5-minute tolerance) mints a code, stores `minted/<sha256>` so the app accepts it, and records `orders/{session id}.json` with the buyer's email and the code. One code per session, even when Stripe retries. **Email is a placeholder**: nothing is sent, so read the order record and email the code by hand. **Unverified**: tested only with a fixture payload signed in the test, never against Stripe.
- **Delete link.** The page after `POST /check` shows the share link and a private delete link (`/c/{id}/delete?key=...`). Only the key's SHA-256 is stored. The link opens a confirm page; the delete itself is a POST. It removes the certificate, its run JSON, the job and its batches, and leaves a `certs/{id}.deleted` marker so the share link answers 410. Empty lease markers under `jobs/{id}/` are left behind.
- **30-day re-check.** `python3 hosted/recheck.py --id {id}` (storage chosen from the same variables as the app) or `python3 hosted/recheck.py run.json` re-runs every claim in a stored run and prints the verdicts that changed. Exit 0 when nothing changed, 1 when something did; `--json` for a machine-readable diff, `--save` to store the new run as `certs/{id}.recheck.json`. Nothing schedules it: run it from cron, and email the owner yourself.
- **Errors.** A storage failure becomes a 503 page and any other exception a 500 page with the traceback in the function log, never a dropped connection.

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
   - A certificate with a test code shows the share and delete links; `/c/{id}` fills in over a few refreshes, and the finished page reloads after a fresh deploy (proves Supabase storage, not local disk).
   - The longest `/c/{id}` request in the function log stays well under 60 s on a draft with 60+ sources.
   - The delete link removes the certificate, and the share link then answers 410.
   - Eleven quick free checks from one address: the eleventh gets 429.
   - Reusing the same code is refused.
   - The function log shows no request path problems with the rewrite (`/c/{id}` must reach the function as `/c/{id}`).
7. Promote to production and add a domain if you want one.
8. **Fulfilment, per sale:** Stripe emails you the payment; reply to the buyer with one unused code. Remove spent codes from `CITED_ACCESS_CODES` from time to time (the app already refuses them).

## Gaps and shortcuts in this slice

- **Untested against live Supabase, Vercel and Stripe.** The Supabase adapter (including delete) is tested against a fake HTTP opener, the Vercel entry point and rewrite are untested until the first preview deploy (step 6), and the Stripe webhook only against a fixture.
- **Jobs need a visitor.** A certificate progresses only while someone has `/c/{id}` open. A buyer who closes the tab at once comes back to a half-done job that resumes on that visit.
- **Re-checks are not scheduled or emailed.** `recheck.py` does the work; cron and the email to the owner are not built.
- **No email.** Neither minted codes nor re-check results are emailed.
- **Leftover keys.** Rate limit slots and job lease markers are never cleaned up; they are empty files.
- **Research notes come back red.** Notes that paraphrase their sources get mostly "Not on page" and "Needs review" (see RUNLOG.md). A table row citing two URLs is checked whole against each of them.
- **Certificates are public by link** until the owner deletes them. There is no index and pages carry `Referrer-Policy: no-referrer`, but anyone given the URL can read it.
