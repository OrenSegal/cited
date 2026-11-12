"""Score cited against hand-labelled claims on a recorded page, with no network.

Each entry in claims.json carries `on_page`: True if a careful reader would say the
page supports the claim, False if it does not. cited "flags" a claim when its tier
blocks (unsupported, low_match, broken). Run: python3 bench/run.py

The 95% intervals resample the 29 claims with replacement (2000 resamples, seed 0).
Claims share one page and one labeller, so they understate the real uncertainty.

Two baselines are scored on the same claims, so the numbers can be read against
something: flagging every claim, and flagging every claim that is not a verbatim
substring of the page (case, punctuation and whitespace ignored).
"""
import collections
import json
import random
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "evals/catch-fabricated-citation/fixtures/source-cache"
gold = json.loads((ROOT / "bench/claims.json").read_text())
proc = subprocess.run(
    [sys.executable, str(ROOT / "skills/cited/scripts/verify_claims.py"), str(ROOT / "bench/claims.json"),
     "--json", "--offline", "--cache", str(CACHE)],
    capture_output=True, text=True)
report = json.loads(proc.stdout)
by_id = {r["id"]: r for r in report["results"]}
tp = fp = fn = tn = 0
outcomes = []  # (on_page, flagged) per claim, for the bootstrap
for g in gold:
    r = by_id[g["id"]]
    flagged = r["blocking"]
    outcomes.append((g["on_page"], flagged))
    if g["on_page"]:
        if flagged:
            fp += 1
            print(f"false positive  {r['tier']:<12} {g['claim']}")
        else:
            tn += 1
    elif flagged:
        tp += 1
    else:
        fn += 1
        print(f"false negative  {r['tier']:<12} {g['claim']}")
absent, present = tp + fn, fp + tn
print(f"\nclaims: {len(gold)} ({absent} not on page, {present} on page)")
print(f"caught {tp} of {absent} absent claims (recall {tp / absent:.2f})")
print(f"flagged {fp} of {present} supported claims (false-positive rate {fp / present:.2f})")
print(f"precision of flags: {tp / (tp + fp):.2f}" if tp + fp else "precision: n/a")


def bootstrap(stat, n=2000, seed=0):
    rng = random.Random(seed)
    vals = sorted(v for v in (stat(rng.choices(outcomes, k=len(outcomes))) for _ in range(n)) if v is not None)
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def recall(rows):
    absent = [f for on_page, f in rows if not on_page]
    return sum(absent) / len(absent) if absent else None


def precision(rows):
    flags = [not on_page for on_page, f in rows if f]
    return sum(flags) / len(flags) if flags else None


lo, hi = bootstrap(recall)
print(f"recall 95% bootstrap CI {lo:.2f}-{hi:.2f}")
lo, hi = bootstrap(precision)
print(f"precision 95% bootstrap CI {lo:.2f}-{hi:.2f}")

print("\ntier by label:")
tiers = collections.Counter((g["on_page"], by_id[g["id"]]["tier"]) for g in gold)
for tier in ("unsupported", "low_match", "verified"):
    print(f"  {tier:<12} not on page {tiers[(False, tier)]:>2}   on page {tiers[(True, tier)]:>2}")

# `unsupported` is the tier the tool calls the fabrication signal; low_match only asks for a person.
outcomes = [(g["on_page"], by_id[g["id"]]["tier"] == "unsupported") for g in gold]
hard = sum(f for on_page, f in outcomes if not on_page)
lo, hi = bootstrap(recall)
print(f"\nunsupported only: {hard} of {absent} absent claims (recall {hard / absent:.2f}, 95% bootstrap CI {lo:.2f}-{hi:.2f}), "
      f"{sum(f for on_page, f in outcomes if on_page)} of {present} supported claims")


def squash(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


pages = {}
for path in CACHE.glob("*.json"):
    entry = json.loads(path.read_text())
    if entry.get("kind") == "page":
        pages[entry["url"]] = squash(entry["data"]["text"])
print("\nbaselines on the same claims (recall, supported claims flagged, precision):")
for name, flag in (("cited", lambda g: by_id[g["id"]]["blocking"]),
                   ("flag unless verbatim", lambda g: squash(g["claim"]) not in pages[g["source_url"]]),
                   ("flag everything", lambda g: True)):
    b_tp = sum(1 for g in gold if not g["on_page"] and flag(g))
    b_fp = sum(1 for g in gold if g["on_page"] and flag(g))
    print(f"  {name:<21} {b_tp} of {absent} ({b_tp / absent:.2f})   {b_fp} of {present} ({b_fp / present:.2f})   "
          f"{b_tp / (b_tp + b_fp):.2f}")
