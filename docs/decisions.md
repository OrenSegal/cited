# Decisions

Each entry is taken from the commit history, merged PRs, the CHANGELOG or
existing docs (README, SKILL.md, `skills/cited/references/methodology.md`,
SECURITY.md, `hosted/DEPLOY-RUNBOOK.md`). Where the record shows the decision
but not the options weighed against it, the entry says "alternative not
recorded".

## 1. Fetch every cited page; do not trust the model or the search snippet

**Context.** An agent writes a claim, attributes it to a source it read,
half-read or reconstructed from a search snippet, and grades its own
confidence with the same model that may have made the claim up.

**Decision.** cited fetches every source URL itself, fresh, with a Wayback
Machine fallback when the live page is gone or empty, and checks the claim
against that page. A site that blocks scripted fetches (HTTP 403/429 or a bot
challenge) with no archived copy comes back `snippet_only`, not verified.

**Alternatives considered and why rejected.**
- Asking the producing model how sure it is: rejected in the README and
  SKILL.md because it does not catch a claim the model made up.
- Treating a bot wall as `broken`: rejected in methodology.md because it would
  fail honest runs whose sources live on Reddit, X or LinkedIn. `snippet_only`
  does not block by default; `--strict` blocks on it.

**Consequences.** cited opens sockets to URLs an agent wrote, so it treats
them as hostile: only `http` and `https`, no private or metadata addresses
(checked on the address actually connected to), capped redirects, size and
time. Proxies from the environment are ignored unless `--proxy-from-env` is
passed, because the proxy would make the connection and bypass the address
check. That default made every fetch fail inside Claude Code's sandbox until
the skill and command learned to pass the flag (PR #10).

**Evidence.** Initial commit fa8e2df; README "Why" and "Security";
SKILL.md; methodology.md "Why platform 403s don't fail the run" and "Why the
Wayback Machine fallback exists"; SECURITY.md; `tests/test_fetch_security.py`;
[PR #10](https://github.com/OrenSegal/cited/pull/10) (a0f2977).

## 2. Containment, not similarity

**Context.** The check asks whether a short claim is on a long page.

**Decision.** Score the claim against the page by dividing only by the length
of the claim. Two signals: `quoted` (word-sequence overlap, which shows
quotation) and `topical` (overlap of distinctive words, which shows only that
the claim is about the same thing).

**Alternatives considered and why rejected.** A symmetric string-similarity
score (common-subsequence ratio, cosine on raw text). Rejected in
methodology.md: those metrics divide by the length of both texts, so a
verbatim quote inside a long page scores near zero.

**Consequences.** Matching is lexical. Negation, qualifiers, numbers written
as words and swapped lowercase words are invisible to it (README
"Limitations"). The labelled benchmark measures that: recall 0.67 on 15
unsupported claims, 5 of 14 supported claims sent to a person. The README
points to an LLM or NLI entailment check as a separate tool for judging
paraphrases; whether one was considered for cited itself is not recorded.

**Evidence.** methodology.md "Why containment, not similarity";
`skills/cited/scripts/tiering_core.py`; `bench/run.py`,
[PR #19](https://github.com/OrenSegal/cited/pull/19) (5318030).

## 3. A claim's numbers and names must be on the page, and `low_match` blocks

**Context.** "Acme Robotics raised a $12M Series A led by Sequoia", checked
against a page that only names the company, scored `low_match` because
"acme" and "robotics" cleared the topical threshold, and SKILL.md said
`low_match` could ship. A quoted claim with one wrong number could also score
`verified` on bigrams.

**Decision.** Before the thresholds apply, every number in the claim (with
its scale word, so `$40 billion` is not `$40 million`), every capitalized
word after the first, and one-letter identifiers such as `Series B` must
appear on the page, or the claim is `unsupported`. `low_match` now fails the
run as "needs review". Later (PR #17), a number in a quoted claim must also
sit within four words, in the same sentence, of a word the claim puts next to
it; if not, the claim is `low_match`.

**Alternatives considered and why rejected.**
- Leaving `low_match` shippable: rejected in PR #1, because shared vocabulary
  is what both a real paraphrase and a fabricated claim about a real company
  look like.
- Making the proximity miss `unsupported`: rejected in PR #17 and
  methodology.md because the proximity check is a heuristic, so it only asks
  for a person.
- Matching numbers anywhere on the page (the behaviour before PR #17):
  disclosed as a limitation in PR #16, then replaced, because "founded in
  2022" came back `verified` on a page that says 2019 and mentions 2022
  elsewhere.

**Consequences.** Pipelines that passed with `low_match` started failing
(stated in PR #1 as intended). No tier flipped on the 23 recorded pairs, but a
claim that moves a number to the front of its sentence goes to review, which
happened in 140 of 196 synthetic rewrites. An invented name as the claim's
first word still passes.

**Evidence.** [PR #1](https://github.com/OrenSegal/cited/pull/1) (305e0ac),
[PR #3](https://github.com/OrenSegal/cited/pull/3) (aba5b27),
[PR #16](https://github.com/OrenSegal/cited/pull/16) (daa4918),
[PR #17](https://github.com/OrenSegal/cited/pull/17) (ccf82e4);
`tests/test_tiering_core.py`; README "Results" and "Limitations".

## 4. Hosted app: demo lane off by default, Buy links hidden without a payment link

**Context.** `hosted/` serves a free link check and a paid certificate behind
single-use access codes. Every code works once, so a visitor had no way to try
a certificate, and the Buy links pointed at a `STRIPE_PAYMENT_LINK_TODO`
placeholder.

**Decision.** `CITED_DEMO_MAX_CLAIMS=N` lets `/check` make a certificate with
no code for a draft of at most N claims, labelled "Demo certificate". It ships
off. Every Buy link is hidden when `CITED_PAYMENT_LINK` is unset. A sample
certificate from a real run is committed under `docs/examples/`.

**Alternatives considered and why rejected.** The runbook lays out the two
choices: keep the $9 gate (free link check only, with the sample certificate
as the preview) or turn on the demo lane with a small N. Neither is rejected;
turning the lane on is left to the owner at deploy time, so the default is the
gate. The placeholder Buy link was replaced by hiding the links.

**Consequences.** The app can be deployed before Stripe exists. With the lane
on, each demo uses a rate-limit slot (10 per address per 10 minutes) and
fetches up to N sources; nothing else bounds it, so N should stay small. The
app is not deployed yet. The copy of cited inside sous ships without
`hosted/` (sous PR #30), so plugin installs never get the app or its Stripe
webhook, which is tested only against a fixture.

**Evidence.** [PR #21](https://github.com/OrenSegal/cited/pull/21) (a6718f6);
[PR #11](https://github.com/OrenSegal/cited/pull/11) (785cb7d);
`hosted/DEPLOY-RUNBOOK.md` section 1; `tests/test_hosted.py`; sous
[PR #30](https://github.com/OrenSegal/sous/pull/30).
