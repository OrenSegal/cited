"""bench/run.py is the numbers the README quotes; fail if they drift."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_bench_numbers_match_readme():
    out = subprocess.run([sys.executable, str(ROOT / "bench/run.py")], capture_output=True, text=True, check=True).stdout
    assert "caught 11 of 15 absent claims" in out
    assert "flagged 6 of 14 supported claims" in out
    assert "unsupported only: 7 of 15 absent claims (recall 0.47, 95% bootstrap CI 0.20-0.73), 0 of 14" in out
    assert "flag unless verbatim  15 of 15 (1.00)   7 of 14 (0.50)   0.68" in out
    assert "flag everything       15 of 15 (1.00)   14 of 14 (1.00)   0.52" in out
    readme = (ROOT / "README.md").read_text()
    assert "flags 11 of the 15" in readme
    assert "catches all 15 and flags 7 of the 14" in readme
