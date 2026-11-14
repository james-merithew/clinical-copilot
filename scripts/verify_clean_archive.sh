#!/usr/bin/env sh
# verify_clean_archive.sh — prove the `git archive` export is self-sufficient.
#
# Reproduces a clean submission zip (GitHub/GitLab "Download ZIP" / tarball), which
# honors `.gitattributes export-ignore`. OpenEMR's inherited rules mark `tests/` and
# `tools/` export-ignore, which (without our carve-outs) strip copilot/app/tools,
# copilot/tests, and redteam/tests -> the eval fails importing app.tools.* BEFORE the
# gate. That was the Week-2-final packaging bug; this guard fails loudly if it returns.
#
# Runs both gates from the EXTRACTED export, not the working tree. Assumes $PYTHON (or
# `python`) already has copilot/requirements.txt installed (CI installs before calling).
#
# Usage:  PYTHON=path/to/python scripts/verify_clean_archive.sh
set -e

PY="${PYTHON:-python}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[clean-archive] exporting HEAD via git archive -> $TMP"
git archive HEAD | ( cd "$TMP" && tar -xf - )

echo "[clean-archive] asserting eval-critical files survived the export"
for f in \
    copilot/app/tools/__init__.py \
    copilot/app/tools/labs.py \
    copilot/tests/conftest.py \
    copilot/tests/ingest/_helpers.py \
    redteam/runner.py \
    redteam/gate.py
do
    if [ ! -f "$TMP/$f" ]; then
        echo "[clean-archive] FAIL: '$f' missing from the export" >&2
        echo "[clean-archive]   -> a .gitattributes export-ignore rule is stripping it;" >&2
        echo "[clean-archive]      add a '<path> -export-ignore' carve-out." >&2
        exit 1
    fi
done

echo "[clean-archive] golden gate (from the export)"
( cd "$TMP" \
  && "$PY" copilot/evals/golden/runner.py --mode hermetic --out copilot/evals/golden/results.json \
  && "$PY" copilot/evals/golden/gate.py \
        --results copilot/evals/golden/results.json \
        --baseline copilot/evals/golden/baseline.json )

echo "[clean-archive] adversarial gate (from the export)"
( cd "$TMP" \
  && "$PY" -m redteam.runner --target hermetic --out evals/adversarial/results.json \
  && "$PY" -m redteam.gate \
        --results evals/adversarial/results.json \
        --baseline evals/adversarial/baseline.json )

echo "[clean-archive] PASS — the export is self-sufficient and both gates pass"
