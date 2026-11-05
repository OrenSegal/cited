"""bench/run.py is the numbers the README quotes; fail if they drift."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_bench_numbers_match_readme():
    out = subprocess.run([sys.executable, str(ROOT / "bench/run.py")], capture_output=True, text=True, check=True).stdout
    assert "caught 10 of 15 absent claims" in out
    assert "flagged 5 of 14 supported claims" in out
    assert "flags 10 of the 15" in (ROOT / "README.md").read_text()
