"""scripts/artefact_version.sh: the version build.yml publishes (docs/releasing.md)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "artefact_version.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(tmp_path: Path, version: str = "0.2.1", **env: str) -> subprocess.CompletedProcess[str]:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nname = "hmcts-otel"\nversion = "{version}"\n\n[tool.ruff]\nversion = "x"\n')
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "PYTHON": sys.executable, "PYPROJECT": str(pyproject), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_draft_base_comes_from_pyproject_not_the_tag(tmp_path: Path) -> None:
    # No v* tag: the action reports 0.0.0-<sha>.
    out = _run(tmp_path, EVENT="push", RUN_NUMBER="47", DRAFT_VERSION="0.0.0-46dfd2f")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0.2.1.dev47+46dfd2f"


def test_draft_ignores_an_older_tag(tmp_path: Path) -> None:
    out = _run(tmp_path, EVENT="push", RUN_NUMBER="3", DRAFT_VERSION="0.1.0-a1b2c3d4")
    assert out.stdout.strip() == "0.2.1.dev3+a1b2c3d4"


def test_draft_without_a_sha(tmp_path: Path) -> None:
    out = _run(tmp_path, EVENT="push", RUN_NUMBER="5", DRAFT_VERSION="")
    assert out.stdout.strip() == "0.2.1.dev5"


def test_release_is_the_clean_pyproject_version(tmp_path: Path) -> None:
    out = _run(tmp_path, EVENT="release", RELEASE_VERSION="0.2.1", RUN_NUMBER="9")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0.2.1"


def test_release_tag_must_match_pyproject(tmp_path: Path) -> None:
    out = _run(tmp_path, EVENT="release", RELEASE_VERSION="0.3.0")
    assert out.returncode != 0 and out.stdout == ""
    assert "does not match" in out.stderr


@pytest.mark.parametrize("bad", ["0.2.1.dev1", "0.2.1+abc", "latest"])
def test_pyproject_must_hold_a_release_version(tmp_path: Path, bad: str) -> None:
    out = _run(tmp_path, bad, EVENT="push", RUN_NUMBER="1", DRAFT_VERSION="0.0.0-46dfd2f")
    assert out.returncode != 0 and out.stdout == ""


def test_the_repository_version_is_a_valid_base(tmp_path: Path) -> None:
    pyproject = SCRIPT.parent.parent / "pyproject.toml"
    out = _run(tmp_path, EVENT="push", RUN_NUMBER="1", DRAFT_VERSION="0.0.0-46dfd2f", PYPROJECT=str(pyproject))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith(".dev1+46dfd2f")
