"""Score cited against hand-labelled claims on a recorded page, with no network.

Each entry in claims.json carries `on_page`: True if a careful reader would say the
page supports the claim, False if it does not. cited "flags" a claim when its tier
blocks (unsupported, low_match, broken). Run: python3 bench/run.py

The 95% intervals resample the 29 claims with replacement (2000 resamples, seed 0).
Claims share one page and one labeller, so they understate the real uncertainty.
"""
import json
import random
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
