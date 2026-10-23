#!/usr/bin/env bash
# Prints the version build.yml publishes (see docs/releasing.md).
#
#   draft (push to main): <pyproject version>.dev<RUN_NUMBER>+<sha>
#   release:              <pyproject version>, which must equal the release tag
#
# The base always comes from pyproject.toml. hmcts/artefact-version-action supplies
# only the commit SHA (DRAFT_VERSION, "<latest tag>-<sha>"; its tag part is ignored,
# as it falls back to 0.0.0 without tags) and the release tag (RELEASE_VERSION).
#
# Environment: EVENT (github.event_name), RUN_NUMBER, DRAFT_VERSION, RELEASE_VERSION,
# PYPROJECT (default pyproject.toml), PYTHON (default python3).
set -euo pipefail

base=$("${PYTHON:-python3}" -c \
  'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
  "${PYPROJECT:-pyproject.toml}")
# A public PEP 440 release version, e.g. 0.2.1 or 1.0.0rc1: no .dev or +local part.
if ! [[ $base =~ ^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?$ ]]; then
  echo "error: pyproject.toml version '$base' is not a release version" >&2
  exit 1
fi

if [ "${EVENT:-}" = "release" ]; then
  if [ "${RELEASE_VERSION:-}" != "$base" ]; then
    echo "error: release tag version '${RELEASE_VERSION:-}' does not match pyproject.toml version '$base'" >&2
    exit 1
  fi
  echo "$base"
  exit 0
fi

if ! [[ ${RUN_NUMBER:-} =~ ^[0-9]+$ ]]; then
  echo "error: RUN_NUMBER '${RUN_NUMBER:-}' is not a number" >&2
  exit 1
fi
sha="${DRAFT_VERSION:-}"
sha="${sha##*-}"
if [[ $sha =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "${base}.dev${RUN_NUMBER}+${sha}"
else
  echo "${base}.dev${RUN_NUMBER}"
fi
