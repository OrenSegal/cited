# Deploy runbook: cited hosted

The owner's steps to put `hosted/webapp.py` on a public URL. Nothing in this repo creates a Vercel or Supabase project; every step below is done by hand in those dashboards. Background and the reasons behind each setting are in [README.md](README.md).

## 1. Decide the demo lane first

`CITED_DEMO_MAX_CLAIMS` is off unless set. With it set to N, anyone can make a certificate with no access code for a draft of at most N claims; the page says "Demo certificate". It is the only way a visitor can try a certificate, because every access code works once.

| Choice | Set | What a visitor gets |
|---|---|---|
| Keep the $9 gate | leave unset | Free link check only; the sample certificate in `docs/examples/` shows what a certificate looks like |
| Demo lane | `CITED_DEMO_MAX_CLAIMS=6` (or another small number) | A real certificate for a short draft, labelled demo; longer drafts still need a code |

Cost of the demo lane: each demo uses one rate-limit slot (10 per address per 10 minutes by default) and fetches up to N sources from the function. Nothing else bounds it, so keep N small.

## 2. Supabase Storage

1. In a Supabase project (new, or an existing non-production one), Storage, New bucket. Name it, for example `certificates`. Leave **Public bucket off** (private).
2. Project Settings, API: copy the **Project URL** and the **service_role** key. The service role key goes only into Vercel's environment variables.

## 3. Stripe (skip while there is no price page)

Without `CITED_PAYMENT_LINK` the app hides every Buy link, so you can deploy before Stripe exists.

1. Product "cited certificate", $9, one-off. Create a Payment Link for it.
2. In the link's confirmation settings, show "We will email your access code within one business day."
3. Optional webhook: endpoint `https://<your domain>/stripe/webhook`, event `checkout.session.completed`; copy the `whsec_...` secret. This path is tested only against a fixture payload.

## 4. Access codes

```sh
python3 -c "import secrets; print(','.join(secrets.token_urlsafe(9) for _ in range(20)))"
```

Keep the list somewhere private; each code works once.

## 5. Vercel import

Settings, from `hosted/README.md` and `vercel.json`:

| Setting | Value |
|---|---|
| Repository | `OrenSegal/cited` |
| Root Directory | blank (the repo root: `api/index.py` imports `skills/cited/scripts/`) |
| Framework preset | Other |
| Build command | none |
| Output directory | none |
| Install command | none (standard library only, no `requirements.txt`) |

`vercel.json` already sets the function (`api/index.py`, `maxDuration` 60) and rewrites every path to it.

## 6. Environment variables

Project Settings, Environment Variables, for Production and Preview.

| Variable | Required | Value |
|---|---|---|
| `SUPABASE_URL` | Yes, for certificates | Project URL from step 2 |
| `SUPABASE_SERVICE_KEY` | Yes, for certificates | service_role key from step 2 |
| `SUPABASE_BUCKET` | Yes, for certificates | Bucket name from step 2 |
| `CITED_ACCESS_CODES` | For paid certificates | Codes from step 4 |
| `CITED_DEMO_MAX_CLAIMS` | Only for the demo lane | Your step 1 decision; leave unset to keep the gate |
| `CITED_PAYMENT_LINK` | For sales | Payment Link from step 3; unset hides the Buy links |
| `STRIPE_WEBHOOK_SECRET` | Only for the webhook | `whsec_...` from step 3 |
| `CITED_RATE_LIMIT` | No | Default `10/600` |

Set all three Supabase variables or none. With none, the free link check still works, and certificate requests (paid or demo) answer 503: on Vercel the app refuses to keep certificates on the function's disk.

## 7. First preview checklist

Deploy a preview (push a branch, or Redeploy in the dashboard) and check, in order. Every item has to pass before promoting.

- [ ] `GET /healthz` returns `ok`.
- [ ] `GET /` shows the form. With `CITED_PAYMENT_LINK` set, "Buy a certificate" points at the Stripe link; without it, no Buy link appears and no `TODO` text shows.
- [ ] With the demo lane on, the code field offers "a free demo certificate of up to N claims".
- [ ] A free link check on a short draft returns a table.
- [ ] A free link check on a draft linking to `http://169.254.169.254/` shows **Refused**.
- [ ] Demo lane on: paste `docs/examples/sample-draft.md` with no code (set N to at least 6). `/c/{id}` fills in and opens with "Demo certificate". A draft with N+1 claims and no code is refused.
- [ ] A certificate with a test code shows the share and delete links; `/c/{id}` fills in over a few refreshes and has no demo note.
- [ ] Redeploy, then reload that certificate: it is still there (Supabase storage, not local disk).
- [ ] Reusing the same code is refused.
- [ ] The delete link removes the certificate, and the share link then answers 410.
- [ ] Eleven quick checks from one address: the eleventh gets 429.
- [ ] In the function log, `/c/{id}` reaches the function as `/c/{id}` (the rewrite keeps the path), and the longest `/c/{id}` request on a draft with 60+ sources stays well under 60 s.
- [ ] Response headers on `/c/{id}` include `Content-Security-Policy: default-src 'none'; ...`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

## 8. Promote

Promote the preview to production, add a domain if wanted, and link the URL from the top of the repo README (replace "It is not deployed"). Add a Supabase lifecycle rule or a periodic prune for `ratelimit/` keys, which are never cleaned up.

Per sale: Stripe emails you the payment; reply to the buyer with one unused code.
