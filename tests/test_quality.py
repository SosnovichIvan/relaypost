import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "base,head,code",
    [
        ("main", "develop", 0),
        ("main", "feat/a", 1),
        ("develop", "feat/a", 0),
        ("develop", "main", 1),
        ("main", "main", 1),
        ("other", "feat/a", 1),
    ],
)
def test_branch_gate(base, head, code):
    result = subprocess.run(  # noqa: S603 - fixed Python executable and repository script
        [sys.executable, str(ROOT / "scripts/check_branch.py")],
        env={**os.environ, "BASE_BRANCH": base, "HEAD_BRANCH": head},
        capture_output=True,
        check=False,
    )
    assert result.returncode == code


@pytest.mark.parametrize("lines,branches,code", [(91, 91, 0), (89, 99, 1), (99, 89, 1)])
def test_coverage_gate(tmp_path, lines, branches, code):
    (tmp_path / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {
                    "num_statements": 100,
                    "covered_lines": lines,
                    "num_branches": 100,
                    "covered_branches": branches,
                }
            }
        )
    )
    result = subprocess.run(  # noqa: S603 - fixed Python executable and repository script
        [sys.executable, str(ROOT / "scripts/check_coverage.py")],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == code
