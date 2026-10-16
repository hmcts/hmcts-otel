"""docs/conventions.md is generated from the schema: fail when it drifts."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_conventions_doc_is_up_to_date():
    result = subprocess.run(
        [sys.executable, "scripts/generate_conventions.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr
