"""Score cited against hand-labelled claims on a recorded page, with no network.

Each entry in claims.json carries `on_page`: True if a careful reader would say the
page supports the claim, False if it does not. cited "flags" a claim when its tier
blocks (unsupported, low_match, broken). Run: python3 bench/run.py
"""
import json
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
for g in gold:
    r = by_id[g["id"]]
    flagged = r["blocking"]
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
