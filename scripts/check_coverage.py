"""Enforce distinct line and branch coverage thresholds."""

import json
from pathlib import Path

report = json.loads(Path("coverage.json").read_text())["totals"]
lines = 100 * report["covered_lines"] / report["num_statements"]
branches = 100 * report["covered_branches"] / report["num_branches"]
print(f"Lines: {lines:.2f}%; branches: {branches:.2f}%")
if lines < 90 or branches < 90:
    raise SystemExit("Required coverage: at least 90% lines AND 90% branches")
